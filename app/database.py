"""SQLite 持久化：批次（预览/发布）状态机 + 受试者撤回登记。

状态机
------
pending  : 已上传，存在可发布的匿名方案，等待确认发布
published: 已确认发布（含可下载 CSV），重启后内容不变
rejected : 已上传但被拒收（无可行方案 / 含已撤回受试者号），或已失效的预览

并发约定
--------
所有多语句写操作都在 ``BEGIN IMMEDIATE`` 事务中执行：事务进入即获得
SQLite 保留锁，事务内读取看到的是最新已提交状态，并发写事务按获得锁的
顺序串行提交。因此「登记撤回 / 上传 / 确认发布」交错时，以事务提交顺序
裁决——先提交者的效果对后提交者可见：

* 撤回先提交：其后的上传事务读到撤回名单（含该号即整批拒收），
  其后的发布事务看到预览已失效（拒绝发布）；
* 上传/确认先提交：撤回事务只影响仍为 pending 的预览，
  已发布文件保持原字节可下载。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"

# insert_upload_batch 的结果类别
UPLOAD_PENDING = "pending"          # 已生成待确认预览
UPLOAD_NO_SOLUTION = "no_solution"  # 无可行匿名方案，不能发布
UPLOAD_WITHDRAWN = "withdrawn"      # 含已撤回受试者号，整批拒收

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL CHECK (status IN ('pending','published','rejected')),
    source_filename TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    plan_json       TEXT,
    output_csv      BLOB,
    output_filename TEXT
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    subject_id  TEXT NOT NULL UNIQUE
);
"""


class WithdrawalExistsError(Exception):
    """该受试者号已登记过撤回（重复登记）。"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # timeout：写锁等待上限（秒），交错事务在此窗口内串行化而非立即失败
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 显式事务控制：见 write_txn
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：进入即获保留锁，异常时整体回滚不留半成品。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 受试者撤回登记
# --------------------------------------------------------------------------
def register_withdrawal(conn: sqlite3.Connection, subject_id: str) -> int:
    """登记撤回：同一事务中写入撤回记录并使全部待确认预览失效。

    已发布批次不受影响；若登记失败（如重复登记触发唯一约束），整个事务
    回滚——待确认预览保持原状，不产生半生效的登记。
    """
    with write_txn(conn):
        try:
            cur = conn.execute(
                "INSERT INTO withdrawals (subject_id) VALUES (?)", (subject_id,))
        except sqlite3.IntegrityError as exc:
            raise WithdrawalExistsError(subject_id) from exc
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        return int(cur.lastrowid)


def list_withdrawals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM withdrawals ORDER BY id DESC"))


def _withdrawn_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["subject_id"] for row in
            conn.execute("SELECT subject_id FROM withdrawals")}


# --------------------------------------------------------------------------
# 批次：上传 / 查询 / 发布
# --------------------------------------------------------------------------
def insert_upload_batch(conn: sqlite3.Connection, filename: str, row_count: int,
                        plan_json: Optional[str], subject_ids: Iterable[str]
                        ) -> tuple[str, int, list[str]]:
    """单事务处理一次上传：撤回检查 → 旧预览失效 → 写入新批次。

    返回 (类别, 批次 id, 命中的撤回号列表)：

    * 源数据含已撤回受试者号 -> (UPLOAD_WITHDRAWN, id, hits)，记录 rejected 批次。
      检查针对源 CSV 的全部受试者号，与匿名方案是否会剔除该行无关；
    * 无可行匿名方案         -> (UPLOAD_NO_SOLUTION, id, [])，记录 rejected 批次；
    * 否则                   -> (UPLOAD_PENDING, id, [])，记录 pending 预览。

    撤回名单在事务内读取：与登记撤回交错时，凡先提交的撤回对本事务可见。
    """
    with write_txn(conn):
        hits = sorted(set(subject_ids) & _withdrawn_ids(conn))
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        if hits:
            cur = conn.execute(
                "INSERT INTO batches (status, source_filename, row_count) "
                "VALUES ('rejected', ?, ?)",
                (filename, row_count),
            )
            return UPLOAD_WITHDRAWN, int(cur.lastrowid), hits
        if plan_json is None:
            cur = conn.execute(
                "INSERT INTO batches (status, source_filename, row_count) "
                "VALUES ('rejected', ?, ?)",
                (filename, row_count),
            )
            return UPLOAD_NO_SOLUTION, int(cur.lastrowid), []
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count, plan_json) "
            "VALUES ('pending', ?, ?, ?)",
            (filename, row_count, plan_json),
        )
        return UPLOAD_PENDING, int(cur.lastrowid), []


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def get_latest_pending(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM batches WHERE status = 'published' ORDER BY id DESC"))


def publish_batch(conn: sqlite3.Connection, batch_id: int,
                  output_csv: bytes, output_filename: str) -> None:
    """原子发布：仅当前 pending 批次可发布；失败抛异常并整体回滚，不留残件。

    状态检查与状态迁移在同一写事务中：与登记撤回交错时，若撤回先提交，
    本事务看到的预览已失效，发布被拒绝；若本事务先提交，撤回不影响
    已发布批次。
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise LookupError("批次不存在")
        if row["status"] != STATUS_PENDING:
            raise RuntimeError(
                "该预览已不可发布（已发布、已被新的上传取代，或因撤回登记失效）")
        conn.execute(
            "UPDATE batches SET status = 'published', output_csv = ?, "
            "output_filename = ? WHERE id = ?",
            (output_csv, output_filename, batch_id),
        )
