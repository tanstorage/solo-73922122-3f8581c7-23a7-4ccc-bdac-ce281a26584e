"""临床数据匿名化发布 Web 应用（FastAPI）。

在匿名预览/发布流程之外，提供受试者撤回共享授权的登记入口：
登记持久保存；登记后已有待确认预览立即失效；此后含该受试者号的源 CSV
整批拒收；已发布文件保持原字节可下载。交错操作按 SQLite 事务提交顺序裁决。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anonymizer as az
from . import database as db

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR.parent / "data" / "anonymizer.db"

MAX_SUBJECT_ID_LEN = 128


def db_path() -> Path:
    return Path(os.environ.get("ANON_DB_PATH", str(DEFAULT_DB)))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db(db_path())
    yield


app = FastAPI(title="临床数据匿名化发布平台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _render(request: Request, template: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request, template, ctx, status_code=status_code)


def _home_context() -> dict:
    with closing(db.connect(db_path())) as conn:
        pending = db.get_latest_pending(conn)
        published = db.list_published(conn)
        withdrawals = db.list_withdrawals(conn)
    pending_plan = az.plan_from_json(pending["plan_json"]) if pending else None
    return {
        "pending": pending,
        "pending_plan": pending_plan,
        "published": published,
        "withdrawals": withdrawals,
        "error": None,
        "info": None,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, withdrawn: str = ""):
    ctx = _home_context()
    if withdrawn:
        ctx["info"] = (
            f"已登记撤回：受试者号 {withdrawn}。已有待确认预览已失效；"
            "此后含该号的源 CSV 将整批拒收；已发布文件不受影响。")
    return _render(request, "index.html", **ctx)


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    filename = file.filename or "未命名.csv"
    try:
        rows = az.parse_csv(raw)
    except az.ValidationError as exc:
        ctx = _home_context()
        ctx["error"] = f"整批拒收：{exc}"
        return _render(request, "index.html", status_code=400, **ctx)

    plan = az.find_best_plan(rows)
    plan_json = az.plan_to_json(plan) if plan is not None else None
    subject_ids = [r.subject_id for r in rows]
    # 单事务：撤回检查 + 旧预览失效 + 写入新批次（与登记撤回按提交顺序裁决）
    with closing(db.connect(db_path())) as conn:
        kind, batch_id, hits = db.insert_upload_batch(
            conn, filename, len(rows), plan_json, subject_ids)

    if kind == db.UPLOAD_WITHDRAWN:
        ctx = _home_context()
        ctx["error"] = (
            "整批拒收：源 CSV 含已撤回共享授权的受试者号："
            f"{'、'.join(hits)}。即使匿名方案会剔除该行，"
            "含该号的批次也不得进入交付流程。")
        return _render(request, "index.html", status_code=422, **ctx)
    if kind == db.UPLOAD_NO_SOLUTION:
        ctx = _home_context()
        ctx["error"] = (
            f"已接收批次 #{batch_id}（{len(rows)} 行），但在至多剔除 2 行、"
            "每组至少 3 人且含至少两种病种的约束下无可行匿名方案，不能发布。")
        return _render(request, "index.html", status_code=422, **ctx)

    return RedirectResponse(url=f"/?preview={batch_id}", status_code=303)


@app.post("/withdrawals")
def register_withdrawal(request: Request, subject_id: str = Form("")):
    sid = subject_id.strip()
    if not sid:
        ctx = _home_context()
        ctx["error"] = "登记失败：受试者号不能为空。待确认预览未受影响。"
        return _render(request, "index.html", status_code=400, **ctx)
    if len(sid) > MAX_SUBJECT_ID_LEN:
        ctx = _home_context()
        ctx["error"] = (
            f"登记失败：受试者号过长（超过 {MAX_SUBJECT_ID_LEN} 字符）。"
            "待确认预览未受影响。")
        return _render(request, "index.html", status_code=400, **ctx)

    with closing(db.connect(db_path())) as conn:
        try:
            db.register_withdrawal(conn, sid)
        except db.WithdrawalExistsError:
            ctx = _home_context()
            ctx["error"] = (
                f"登记失败：受试者号 {sid} 已在撤回名单中，本次登记未生效。"
                "待确认预览未受影响。")
            return _render(request, "index.html", status_code=409, **ctx)

    return RedirectResponse(url=f"/?withdrawn={quote(sid)}", status_code=303)


@app.post("/publish/{batch_id}")
def publish(request: Request, batch_id: int, confirm: str = Form("")):
    if confirm != "on":
        ctx = _home_context()
        ctx["error"] = "发布前必须勾选确认框"
        return _render(request, "index.html", status_code=400, **ctx)

    with closing(db.connect(db_path())) as conn:
        try:
            row = db.get_batch(conn, batch_id)
            if row is None:
                raise LookupError("批次不存在")
            plan = az.plan_from_json(row["plan_json"])
            if plan is None or not az.verify_output(plan["output_rows"]):
                raise RuntimeError("匿名结果未通过发布前复核")
            output_csv = az.render_output_csv(plan["output_rows"])
            output_filename = f"anonymous_batch_{batch_id}.csv"
            db.publish_batch(conn, batch_id, output_csv, output_filename)
        except (LookupError, RuntimeError) as exc:
            ctx = _home_context()
            ctx["error"] = str(exc)
            return _render(request, "index.html", status_code=409, **ctx)

    return RedirectResponse(url=f"/?published={batch_id}", status_code=303)


@app.get("/download/{batch_id}")
def download(batch_id: int):
    with closing(db.connect(db_path())) as conn:
        row = db.get_batch(conn, batch_id)
        if row is None or row["status"] != db.STATUS_PUBLISHED:
            return Response("文件不存在或尚未发布", status_code=404,
                            media_type="text/plain; charset=utf-8")
        fname = row["output_filename"]
        return Response(
            content=row["output_csv"],
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f"attachment; filename={fname}; filename*=UTF-8''{quote(fname)}"
            },
        )
