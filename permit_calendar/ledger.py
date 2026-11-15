"""进场依据留档：把某次效力查询固化为不可变记录。

快照写入后只允许读取与完整性复核，没有更新、删除接口；后续采集到的
更正、撤回、重发进入同一个原始归档，但不会改写任何已保存快照。
快照的输入边界是一组 capture_id，因此可以在任何时候用同一批原始观察
重新派生视图并比对摘要。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .lifecycle import view_fingerprint


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                saved_at TEXT NOT NULL,
                purpose TEXT NOT NULL,
                business_time TEXT NOT NULL,
                observed_by TEXT,
                capture_ids TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                view_json TEXT NOT NULL
            );
        """)

    def close(self):
        self.connection.close()

    def save(self, view: dict, purpose: str, capture_ids: list[int]) -> dict:
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("purpose is required")
        if not capture_ids:
            raise ValueError("a snapshot must be grounded on at least one capture")
        fingerprint = view_fingerprint(view)
        saved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO snapshots(saved_at,purpose,business_time,observed_by,"
                "capture_ids,fingerprint,view_json) VALUES (?,?,?,?,?,?,?)",
                (saved_at, purpose.strip(), view["business_time"]["value"],
                 view["observed_by"], json.dumps(sorted(capture_ids)),
                 fingerprint, json.dumps(view, ensure_ascii=False, sort_keys=True)),
            )
        return {"snapshot_id": cursor.lastrowid, "saved_at": saved_at,
                "fingerprint": fingerprint, "capture_ids": sorted(capture_ids)}

    def get(self, snapshot_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        view = json.loads(row["view_json"])
        return {"id": row["id"], "saved_at": row["saved_at"], "purpose": row["purpose"],
                "business_time": row["business_time"], "observed_by": row["observed_by"],
                "capture_ids": json.loads(row["capture_ids"]),
                "fingerprint": row["fingerprint"], "view": view}

    def list_snapshots(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id,saved_at,purpose,business_time,observed_by,capture_ids,fingerprint "
            "FROM snapshots ORDER BY id").fetchall()
        return [{"id": row["id"], "saved_at": row["saved_at"], "purpose": row["purpose"],
                 "business_time": row["business_time"], "observed_by": row["observed_by"],
                 "capture_ids": json.loads(row["capture_ids"]),
                 "fingerprint": row["fingerprint"]} for row in rows]

    def verify(self, snapshot_id: int, archive) -> dict:
        """用快照声明的同一批 capture 重新派生，核对留档是否被改写。

        重新派生只读取原始归档；新增的撤回/重发因 capture_id 不在边界内
        而不会参与计算，这正是“后续更正不能改写进场依据”的保证。
        """
        from .lifecycle import build_view, load_observations

        record = self.get(snapshot_id)
        allowed = set(record["capture_ids"])

        class ScopedArchive:
            def __init__(self, parent):
                self.parent = parent

            def captures(self, source=None):
                return [row for row in self.parent.captures(source) if row["id"] in allowed]

            def body(self, capture_id):
                if capture_id not in allowed:
                    raise KeyError(capture_id)
                return self.parent.body(capture_id)

        observed_by = None
        if record["observed_by"]:
            from .lifecycle import parse_observed_at
            observed_by = parse_observed_at(record["observed_by"])
        from .lifecycle import parse_stamp
        business_time = parse_stamp(record["business_time"], "second")
        from datetime import datetime as _dt
        rebuilt = build_view(load_observations(ScopedArchive(archive)),
                             business_time=_dt.fromisoformat(
                                 business_time.value.replace("Z", "+00:00")),
                             observed_by=observed_by)
        rebuilt_fingerprint = view_fingerprint(rebuilt)
        stored = json.loads(self.connection.execute(
            "SELECT view_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()["view_json"])
        stored_fingerprint = view_fingerprint(stored)
        return {
            "snapshot_id": snapshot_id,
            "stored_fingerprint": record["fingerprint"],
            "stored_view_fingerprint": stored_fingerprint,
            "rebuilt_fingerprint": rebuilt_fingerprint,
            "intact": (record["fingerprint"] == stored_fingerprint
                       and rebuilt_fingerprint == stored_fingerprint),
            "capture_ids": sorted(allowed),
        }
