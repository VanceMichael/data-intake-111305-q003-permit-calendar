"""派生解释层：把原始归档重放为声明，并支撑效力判断与留档。

派生库是原始归档的确定性重放结果，可随时删除重建；原始归档不变。
判断不在写入时固化，而是在查询时按知识截止即时计算，因此历史时点
查询与当前查询走同一条代码路径。留档（snapshots）是唯一固化判断的
地方，写入后不可改写。
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .decoding import records as decode_records
from .lifecycle import judge, statement_from_record

SCOPES = ("enumeration", "singleton", "confirmation", "unknown")


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_instant(text):
    return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class DerivedStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS statements (
                id INTEGER PRIMARY KEY,
                capture_id INTEGER NOT NULL,
                item_index INTEGER NOT NULL,
                authority TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                edition TEXT NOT NULL,
                kind TEXT,
                replaces TEXT,
                published TEXT,
                precision TEXT,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(capture_id, item_index)
            );
            CREATE TABLE IF NOT EXISTS incorporated (
                capture_id INTEGER PRIMARY KEY,
                note TEXT NOT NULL,
                statements INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capture_scope (
                capture_id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL CHECK(scope IN ('enumeration','singleton','confirmation','unknown')),
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                purpose TEXT NOT NULL,
                authority TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                cutoff_capture_id INTEGER NOT NULL,
                judgment TEXT NOT NULL,
                digest TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
            BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
            BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
        """)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # ---- 重放：原始归档 -> 声明 ----

    def incorporated_ids(self):
        rows = self.connection.execute("SELECT capture_id FROM incorporated").fetchall()
        return {row["capture_id"] for row in rows}

    def max_incorporated(self):
        row = self.connection.execute("SELECT max(capture_id) AS m FROM incorporated").fetchone()
        return row["m"] or 0

    def incorporate(self, archive):
        """把归档中尚未解释的捕获重放为声明。返回本次处理摘要。"""
        done = self.incorporated_ids()
        summary = {"incorporated": 0, "statements": 0, "notes": {}}
        for capture in archive.captures():
            capture_id = capture["id"]
            if capture_id in done:
                continue
            note, count, statements = self._incorporate_capture(archive, capture)
            with self.transaction() as connection:
                for statement in statements:
                    connection.execute(
                        """INSERT INTO statements
                           (capture_id,item_index,authority,notice_id,edition,kind,replaces,
                            published,precision,observed_at,payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            statement["capture_id"], statement["item_index"],
                            statement["authority"], statement["notice_id"], statement["edition"],
                            statement["kind"], statement["replaces"],
                            statement["published"], statement["precision"],
                            statement["observed_at"], _canonical(statement["payload"]),
                        ),
                    )
                connection.execute(
                    "INSERT INTO incorporated VALUES (?, ?, ?)", (capture_id, note, count)
                )
            summary["incorporated"] += 1
            summary["statements"] += count
            summary["notes"][capture_id] = note
        return summary

    def _incorporate_capture(self, archive, capture):
        """解释单次捕获，返回 (备注, 声明数, 声明列表)。不写库。"""
        capture_id, status = capture["id"], capture["status"]
        if status != 200:
            if status == 304:
                self.set_scope(capture_id, "confirmation", note="内容未变确认", only_if_absent=True)
            return "status_%d" % status, 0, []
        body = archive.body(capture_id)
        content_type = capture["headers"].get("content-type", "")
        try:
            items = decode_records(body, content_type)
        except ValueError as error:
            return "decode_error: %s" % error, 0, []
        statements, problems = [], []
        for index, item in enumerate(items):
            try:
                statements.append(statement_from_record(item, capture_id, index, capture["received_at"]))
            except ValueError as error:
                problems.append("item %d: %s" % (index, error))
        note = "ok" if not problems else "skipped: " + "; ".join(problems)
        return note, len(statements), statements

    # ---- 覆盖口径 ----

    def set_scope(self, capture_id, scope, note=None, only_if_absent=False):
        if scope not in SCOPES:
            raise ValueError("scope must be one of: " + ", ".join(SCOPES))
        if only_if_absent:
            self.connection.execute(
                "INSERT OR IGNORE INTO capture_scope VALUES (?, ?, ?)", (capture_id, scope, note)
            )
        else:
            self.connection.execute(
                "INSERT INTO capture_scope VALUES (?, ?, ?) "
                "ON CONFLICT(capture_id) DO UPDATE SET scope=excluded.scope, note=excluded.note",
                (capture_id, scope, note),
            )
        self.connection.commit()

    def scopes(self):
        rows = self.connection.execute("SELECT * FROM capture_scope ORDER BY capture_id").fetchall()
        return [dict(row) for row in rows]

    # ---- 查询 ----

    def resolve_cutoff(self, archive, as_of_capture=None, as_of_time=None):
        """把查询口径换算为知识截止捕获号。默认取已解释的最大捕获号。"""
        if as_of_capture is not None and as_of_time is not None:
            raise ValueError("as_of_capture and as_of_time are mutually exclusive")
        if as_of_capture is not None:
            return int(as_of_capture)
        if as_of_time is not None:
            instant = _parse_instant(as_of_time)
            eligible = [c["id"] for c in archive.captures() if _parse_instant(c["received_at"]) <= instant]
            return max(eligible) if eligible else 0
        return self.max_incorporated()

    def _statements(self, authority, notice_id, cutoff):
        rows = self.connection.execute(
            """SELECT * FROM statements
               WHERE authority=? AND notice_id=? AND capture_id<=?
               ORDER BY capture_id, item_index""",
            (authority, notice_id, cutoff),
        ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def _enumerations(self, archive, authority, notice_id, cutoff):
        """汇总完整列表观察：本组出现的版本集合及后续内容未变确认。"""
        scopes = {row["capture_id"]: row["scope"] for row in self.connection.execute(
            "SELECT capture_id, scope FROM capture_scope WHERE capture_id<=?", (cutoff,)
        )}
        enumerations = []
        confirmations_by_etag = {}
        for capture in archive.captures():
            if capture["id"] > cutoff:
                break
            etag = capture["headers"].get("etag")
            if capture["status"] == 304 and etag:
                confirmations_by_etag.setdefault(etag, []).append(capture["id"])
                continue
            if capture["status"] != 200 or scopes.get(capture["id"]) != "enumeration":
                continue
            try:
                items = decode_records(archive.body(capture["id"]), capture["headers"].get("content-type", ""))
            except ValueError:
                continue
            editions = {
                str(item["edition"]).strip()
                for item in items
                if isinstance(item, dict)
                and str(item.get("authority", "")).strip() == authority
                and str(item.get("notice_id", "")).strip() == notice_id
                and item.get("edition") is not None
            }
            enumerations.append({
                "capture_id": capture["id"],
                "editions": editions,
                "etag": etag,
                "confirmations": [],
            })
        for enumeration in enumerations:
            if enumeration["etag"] in confirmations_by_etag:
                enumeration["confirmations"] = confirmations_by_etag[enumeration["etag"]]
        return enumerations

    def judgment(self, archive, authority, notice_id, cutoff=None, with_originals=False):
        """按知识截止计算效力判断。cutoff 为 None 时取已解释的最大捕获号。"""
        if cutoff is None:
            cutoff = self.max_incorporated()
        statements = self._statements(authority, notice_id, cutoff)
        enumerations = self._enumerations(archive, authority, notice_id, cutoff)
        result = judge(authority, notice_id, statements, enumerations, as_of_capture=cutoff)
        if with_originals:
            result["originals"] = [
                {
                    "statement_id": s["id"],
                    "capture_id": s["capture_id"],
                    "observed_at": s["observed_at"],
                    "payload": s["payload"],
                }
                for s in statements
            ]
        return result

    def known_groups(self):
        rows = self.connection.execute(
            "SELECT DISTINCT authority, notice_id FROM statements ORDER BY authority, notice_id"
        ).fetchall()
        return [(row["authority"], row["notice_id"]) for row in rows]

    # ---- 留档 ----

    def save_snapshot(self, archive, authority, notice_id, purpose):
        """把当前时点的判断固化为不可改写留档。返回留档号。"""
        if not purpose or not str(purpose).strip():
            raise ValueError("purpose is required")
        cutoff = self.max_incorporated()
        judgment = self.judgment(archive, authority, notice_id, cutoff=cutoff, with_originals=True)
        record = {
            "query": {"authority": authority, "notice_id": notice_id},
            "cutoff_capture_id": cutoff,
            "judgment": judgment,
        }
        digest = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO snapshots (created_at,purpose,authority,notice_id,cutoff_capture_id,judgment,digest)"
                " VALUES (?,?,?,?,?,?,?)",
                (_utcnow(), str(purpose).strip(), authority, notice_id, cutoff, _canonical(judgment), digest),
            )
        return cursor.lastrowid

    def snapshots(self):
        rows = self.connection.execute(
            "SELECT id,created_at,purpose,authority,notice_id,cutoff_capture_id,judgment,digest"
            " FROM snapshots ORDER BY id"
        ).fetchall()
        return [
            {**{k: row[k] for k in row.keys() if k != "judgment"},
             "state": json.loads(row["judgment"])["state"]}
            for row in rows
        ]

    def snapshot(self, snapshot_id):
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "purpose": row["purpose"],
            "authority": row["authority"],
            "notice_id": row["notice_id"],
            "cutoff_capture_id": row["cutoff_capture_id"],
            "judgment": json.loads(row["judgment"]),
            "digest": row["digest"],
        }

    def verify_snapshot(self, archive, snapshot_id):
        """按留档的知识截止重算，校验留档未被改写、判断可复现。"""
        saved = self.snapshot(snapshot_id)
        judgment = self.judgment(
            archive, saved["authority"], saved["notice_id"],
            cutoff=saved["cutoff_capture_id"], with_originals=True,
        )
        record = {
            "query": {"authority": saved["authority"], "notice_id": saved["notice_id"]},
            "cutoff_capture_id": saved["cutoff_capture_id"],
            "judgment": judgment,
        }
        digest = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
        return {
            "snapshot_id": snapshot_id,
            "intact": digest == saved["digest"],
            "saved_digest": saved["digest"],
            "recomputed_digest": digest,
        }
