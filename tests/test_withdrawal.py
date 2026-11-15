"""受试者撤回授权：登记持久化、整批拒收、预览失效、已发布不受影响、
登记失败不改状态、重启后仍阻断，以及与上传/发布交错时按事务提交顺序裁决。"""
import importlib
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from app import anonymizer as az
from app import database as db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ANON_DB_PATH", str(db_file))
    # 确保拿到读取新环境变量的模块实例
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_file


def _csv_bytes(rows, header="受试者号,年龄,邮编,病种"):
    lines = [header] + [",".join(map(str, r)) for r in rows]
    return ("\n".join(lines)).encode("utf-8-sig")


def _group(prefix, age, zipc):
    return [(f"{prefix}{i}", age, zipc,
             "糖尿病" if i % 2 == 0 else "高血压") for i in range(3)]


VALID = _group("A", 40, "10000") + _group("B", 50, "10000")
OTHER = _group("C", 60, "30000") + _group("D", 70, "40000")
# 该数据的可行方案会剔除 X1、X2 两个落单行（见 test_anonymizer.test_removal_at_most_two）
STRAGGLERS = (_group("A", 40, "10000")
              + [("X1", 99, "99999", "罕见病"), ("X2", 88, "88888", "罕见病")])


def _upload(c, rows, name="d.csv"):
    return c.post("/upload", files={"file": (name, _csv_bytes(rows), "text/csv")},
                  follow_redirects=False)


def _register(c, sid):
    return c.post("/withdrawals", data={"subject_id": sid},
                  follow_redirects=False)


# ------------------------- 登记与名单展示 -------------------------
def test_register_withdrawal_shows_in_list(client):
    c, _ = client
    r = _register(c, "S-001")
    assert r.status_code == 303
    home = c.get("/").text
    assert "S-001" in home and "已登记撤回的受试者号" in home


def test_register_strips_whitespace(client):
    c, _ = client
    assert _register(c, "  S-002  ").status_code == 303
    # 去空白后按精确值阻断
    r = _upload(c, [("S-002", 40, "10000", "糖尿病")] + VALID)
    assert r.status_code == 422 and "整批拒收" in r.text


# ------------------------- 上传整批拒收 -------------------------
def test_upload_containing_withdrawn_subject_rejected(client):
    c, _ = client
    _register(c, "A0")
    r = _upload(c, VALID)  # VALID 含 A0
    assert r.status_code == 422
    assert "整批拒收" in r.text and "撤回" in r.text and "A0" in r.text
    # 拒收后不存在待确认预览，也无任何可下载文件
    assert "确认发布" not in c.get("/").text
    assert c.get("/download/1").status_code == 404


def test_rejection_even_when_plan_would_remove_the_row(client):
    c, _ = client
    # 对照：未登记撤回时，含 X1 的数据可上传（方案会剔除 X1、X2）
    assert _upload(c, STRAGGLERS).status_code == 303
    # 登记 X1 后，同一文件即使方案会剔除 X1 所在行，也整批拒收
    _register(c, "X1")
    r = _upload(c, STRAGGLERS)
    assert r.status_code == 422
    assert "整批拒收" in r.text and "X1" in r.text
    assert "确认发布" not in c.get("/").text


def test_upload_without_withdrawn_subject_still_works(client):
    c, _ = client
    _register(c, "ZZZ")  # 与数据无关的撤回号不影响正常上传
    assert _upload(c, VALID).status_code == 303
    assert "确认发布" in c.get("/").text


# ------------------------- 登记使待确认预览失效 -------------------------
def test_registration_invalidates_pending_preview(client):
    c, _ = client
    _upload(c, VALID)
    assert "确认发布" in c.get("/").text
    _register(c, "NOBODY")  # 即使该号不在预览批次中，预览也失效
    assert "确认发布" not in c.get("/").text
    r = c.post("/publish/1", data={"confirm": "on"})
    assert r.status_code == 409 and "不可发布" in r.text
    assert c.get("/download/1").status_code == 404  # 无残件


def test_published_file_untouched_by_withdrawal(client):
    c, _ = client
    _upload(c, VALID)
    c.post("/publish/1", data={"confirm": "on"}, follow_redirects=False)
    before = c.get("/download/1").content
    # 确认先提交后登记撤回：已发布文件保持原字节可下载
    _register(c, "A0")
    after = c.get("/download/1")
    assert after.status_code == 200
    assert after.content == before
    assert "anonymous_batch_1.csv" in c.get("/").text


# ------------------------- 登记失败不改变待确认状态 -------------------------
def test_empty_subject_id_rejected_and_pending_kept(client):
    c, _ = client
    _upload(c, VALID)
    r = _register(c, "   ")
    assert r.status_code == 400 and "登记失败" in r.text
    assert "确认发布" in c.get("/").text  # 待确认预览保持原状
    assert c.post("/publish/1", data={"confirm": "on"},
                  follow_redirects=False).status_code == 303


def test_duplicate_registration_rejected_and_pending_kept(client):
    c, _ = client
    _register(c, "A0")
    _upload(c, OTHER)  # OTHER 不含 A0，可正常生成预览
    assert "确认发布" in c.get("/").text
    r = _register(c, "A0")  # 重复登记
    assert r.status_code == 409 and "登记失败" in r.text
    assert "确认发布" in c.get("/").text  # 待确认预览未受影响
    assert c.post("/publish/1", data={"confirm": "on"},
                  follow_redirects=False).status_code == 303


# ------------------------- 重启后仍阻断 -------------------------
def test_withdrawal_persists_and_blocks_across_restart(client):
    c, db_file = client
    _register(c, "A0")

    # 模拟重启：新建应用实例指向同一个 SQLite 文件
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c2:
        home = c2.get("/").text
        assert "A0" in home  # 撤回名单持久可见
        r = _upload(c2, VALID)  # 含 A0，重启后仍整批拒收
        assert r.status_code == 422 and "整批拒收" in r.text
        assert _upload(c2, OTHER).status_code == 303  # 不含该号则不受影响


# ------------------------- 并发交错：按事务提交顺序裁决 -------------------------
def _fresh_db(tmp_path, name):
    db_file = tmp_path / name
    db.init_db(db_file)
    return db_file


def _run_pair(fn_a, fn_b):
    barrier = threading.Barrier(2)
    errors = []

    def wrap(fn):
        def inner():
            barrier.wait()
            try:
                fn()
            except Exception as exc:  # pragma: no cover - 失败时输出
                errors.append(exc)
        return inner

    threads = [threading.Thread(target=wrap(fn_a)),
               threading.Thread(target=wrap(fn_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_withdrawal_and_upload_race_never_leaves_publishable_preview(tmp_path):
    """撤回与上传并发：无论谁先提交，最终都不存在可发布的含该号预览。"""
    rows = az.parse_csv(_csv_bytes(VALID))  # 含 A0
    plan_json = az.plan_to_json(az.find_best_plan(rows))
    ids = [r.subject_id for r in rows]

    for i in range(20):
        db_file = _fresh_db(tmp_path, f"race_upload_{i}.db")

        def do_withdraw():
            with db.connect(db_file) as conn:
                db.register_withdrawal(conn, "A0")

        def do_upload():
            with db.connect(db_file) as conn:
                db.insert_upload_batch(conn, "d.csv", len(rows), plan_json, ids)

        _run_pair(do_withdraw, do_upload)

        with db.connect(db_file) as conn:
            # 撤回先提交 -> 上传被整批拒收；上传先提交 -> 预览随即被撤回失效
            assert db.get_latest_pending(conn) is None
            assert len(db.list_withdrawals(conn)) == 1


def test_withdrawal_and_publish_race_respects_commit_order(tmp_path):
    """撤回与确认并发：撤回先提交则发布失败且无残件；
    确认先提交则已发布文件保持原字节。"""
    rows = az.parse_csv(_csv_bytes(VALID))
    plan_json = az.plan_to_json(az.find_best_plan(rows))
    ids = [r.subject_id for r in rows]

    for i in range(20):
        db_file = _fresh_db(tmp_path, f"race_publish_{i}.db")
        with db.connect(db_file) as conn:
            kind, batch_id, _ = db.insert_upload_batch(
                conn, "d.csv", len(rows), plan_json, ids)
            assert kind == db.UPLOAD_PENDING

        outcome = {}

        def do_withdraw():
            with db.connect(db_file) as conn:
                db.register_withdrawal(conn, "A0")

        def do_publish():
            with db.connect(db_file) as conn:
                try:
                    db.publish_batch(conn, batch_id, b"CSV-BYTES", "out.csv")
                    outcome["published"] = True
                except RuntimeError:
                    outcome["published"] = False

        _run_pair(do_withdraw, do_publish)

        with db.connect(db_file) as conn:
            batch = db.get_batch(conn, batch_id)
            if outcome["published"]:
                # 确认先提交：已发布文件原字节可下载，撤回不影响
                assert batch["status"] == db.STATUS_PUBLISHED
                assert bytes(batch["output_csv"]) == b"CSV-BYTES"
            else:
                # 撤回先提交：发布被拒绝，不留可下载残件
                assert batch["status"] == db.STATUS_REJECTED
                assert batch["output_csv"] is None
            assert len(db.list_withdrawals(conn)) == 1
