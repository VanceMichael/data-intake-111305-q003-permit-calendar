import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from permit_calendar.archive import Archive
from permit_calendar.client import Client, collect
from permit_calendar.lifecycle import (
    build_view, compare_stamps, extract_statements, load_observations,
    parse_stamp, replacement_status,
)
from permit_calendar.ledger import Ledger
from permit_calendar.origin import serve


def stamp(day, precision="day"):
    return parse_stamp(day, precision)


def make_record(authority, notice_id, edition, kind, published, *,
                replaces=None, start=None, text=None, cites=None, scope=None):
    record = {"authority": authority, "notice_id": notice_id, "edition": edition,
              "kind": kind, "published": published, "precision": "day"}
    if replaces is not None:
        record["replaces"] = replaces
    if start is not None:
        record["start"] = start
    if text is not None:
        record["text"] = text
    if cites is not None:
        record["cites"] = cites
    if scope is not None:
        record["scope"] = scope
    return record


def statements_from(records, first_capture=1, base_time="2026-11-10T08:00:00Z"):
    statements = []
    rejections = []
    base = datetime.fromisoformat(base_time.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    for offset, record in enumerate(records):
        observed_at = (base + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
        batch = extract_statements(
            [record], "unit-source", first_capture + offset,
            observed_at, f"/unit/{first_capture + offset}")
        statements.extend(batch["statements"])
        rejections.extend(batch["rejections"])
    return statements, rejections


def view_of(records, *, business_time="2026-11-20T00:00:00Z", observed_by=None, probes=None):
    statements, rejections = statements_from(records)
    observations = {"statements": statements, "rejections": rejections,
                    "probes": probes or []}
    business = datetime.fromisoformat(business_time.replace("Z", "+00:00"))
    return build_view(observations, business_time=business, observed_by=observed_by)


def north_thread(view):
    threads = [item for item in view["threads"]
               if item["authority"] == "north" and item["notice_id"] == "P26-8"]
    assert len(threads) == 1
    return threads[0]


class StampTests(unittest.TestCase):
    def test_day_precision_spans_whole_day_and_overlaps_are_undecidable(self):
        left = stamp("2026-11-03")
        right = stamp("2026-11-03T14", "hour")
        self.assertEqual(compare_stamps(left, right), None)
        self.assertEqual(compare_stamps(stamp("2026-11-03"), stamp("2026-11-04")), -1)
        self.assertEqual(compare_stamps(stamp("2026-11-03T10", "hour"),
                                        stamp("2026-11-03T12", "hour")), -1)
        self.assertEqual(compare_stamps(stamp("2026-11-03T10", "hour"), None), None)

    def test_precision_cannot_be_finer_than_text_granularity(self):
        self.assertEqual(stamp("2026-11-03T14", "hour").as_dict()["precision"], "hour")
        with self.assertRaisesRegex(ValueError, "finer than text granularity"):
            stamp("2026-11-03T14:00", "hour")
        with self.assertRaisesRegex(ValueError, "finer than text granularity"):
            stamp("2026-11-03T14:00:00", "minute")

    def test_precision_is_preserved_in_output(self):
        self.assertEqual(stamp("2026-11-03").as_dict()["precision"], "day")
        self.assertEqual(stamp("2026-11-03T10:30:00", "second").as_dict()["precision"], "second")


class LifecycleTests(unittest.TestCase):
    def chain_records(self):
        return [
            make_record("north", "P26-8", "first", "issued", "2026-11-01",
                        start="2026-11-10"),
            make_record("north", "P26-8", "withdraw", "withdrawn", "2026-11-03",
                        replaces="first", text="撤回 P26-8 首次发布"),
            make_record("north", "P26-8", "reissue", "issued", "2026-11-03",
                        replaces="withdraw", start="2026-11-17"),
        ]

    def test_full_chain_ends_effective_and_keeps_withdrawal_relation(self):
        view = view_of(self.chain_records())
        thread = north_thread(view)
        self.assertEqual(thread["status"], "effective")
        self.assertEqual(thread["head_edition"], "reissue")
        editions = [node["edition"] for node in thread["chain"]]
        self.assertEqual(editions, ["first", "withdraw", "reissue"])
        self.assertEqual(thread["chain"][1]["raw"]["text"], "撤回 P26-8 首次发布")
        self.assertEqual(thread["chain"][2]["replaces"], "withdraw")

    def test_withdrawal_only_is_withdrawn(self):
        view = view_of(self.chain_records()[:2])
        self.assertEqual(north_thread(view)["status"], "withdrawn")

    def test_reissue_with_future_start_is_scheduled(self):
        view = view_of(self.chain_records(), business_time="2026-11-15T00:00:00Z")
        self.assertEqual(north_thread(view)["status"], "scheduled")

    def test_same_day_unlinked_pair_stays_pending_with_evidence(self):
        records = [
            make_record("north", "P26-8", "a", "issued", "2026-11-03",
                        text="版本甲"),
            make_record("north", "P26-8", "b", "issued", "2026-11-03",
                        text="版本乙"),
        ]
        thread = north_thread(view_of(records))
        # 日级精度区间重叠 -> 不可判定，线程保持待核，不得按抓取顺序排序。
        self.assertEqual(thread["status"], "pending_verification")
        conflict = thread["conflicts"][0]
        self.assertEqual(conflict["code"], "unlinked_same_day")
        self.assertEqual({item["raw"]["text"] for item in conflict["statements"]},
                         {"版本甲", "版本乙"})

    def test_late_cached_old_page_does_not_revive_withdrawal(self):
        # 同载荷的迟到旧页面：reobservation，撤回仍然成立。
        records = self.chain_records()[:2] + [
            make_record("north", "P26-8", "first", "issued", "2026-11-01",
                        start="2026-11-10"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "withdrawn")
        self.assertEqual(thread["reobservations"][0]["edition"], "first")

        # 互不引用的晚到新载荷：声明日期可判定晚于撤回，且确实在撤回后才观察到。
        records = self.chain_records()[:2] + [
            make_record("north", "P26-8", "cache-copy", "issued", "2026-11-04"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "conflict")
        self.assertIn("late_unlinked_issuance", [item["code"] for item in thread["conflicts"]])

        # 同日且互不引用的迟到页面：精度不足，保持待核而非自动恢复。
        records = self.chain_records()[:2] + [
            make_record("north", "P26-8", "same-day-copy", "issued", "2026-11-03"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "pending_verification")
        self.assertEqual(thread["conflicts"][0]["code"], "unlinked_same_day")

    def test_true_reissue_restores_effectiveness_through_reference(self):
        thread = north_thread(view_of(self.chain_records()))
        answer = replacement_status(thread, "withdraw")
        self.assertEqual(answer["status"], "observed")
        self.assertEqual(answer["replacement"]["edition"], "reissue")

    def test_authorities_sharing_notice_id_never_merge(self):
        records = self.chain_records() + [
            make_record("south", "P26-8", "s1", "issued", "2026-11-03"),
        ]
        view = view_of(records)
        keys = {(item["authority"], item["notice_id"]) for item in view["threads"]}
        self.assertIn(("north", "P26-8"), keys)
        self.assertIn(("south", "P26-8"), keys)
        south = next(item for item in view["threads"] if item["authority"] == "south")
        self.assertEqual(south["status"], "effective")

    def test_not_seen_versus_confirmed_absent(self):
        records = self.chain_records()[:2]
        thread = north_thread(view_of(records))
        self.assertEqual(replacement_status(thread, "withdraw")["status"], "not_seen")

        absent = make_record("north", "P26-8", "absent-1", "absent", "2026-11-05",
                             scope="replacement", text="撤回后不存在替代公告")
        thread = north_thread(view_of(records + [absent]))
        answer = replacement_status(thread, "withdraw")
        self.assertEqual(answer["status"], "confirmed_absent")
        self.assertEqual(answer["evidence"][0]["raw"]["text"], "撤回后不存在替代公告")

    def test_dangling_reference_is_pending_not_guessed(self):
        records = [
            make_record("north", "P26-8", "second", "issued", "2026-11-05",
                        replaces="missing-first"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "pending_verification")
        self.assertEqual(thread["unresolved_edges"][0]["target_edition"], "missing-first")

    def test_contradictory_editions_surface_conflict_and_raw_text(self):
        statements, _ = statements_from([
            make_record("north", "P26-8", "same", "issued", "2026-11-01", text="原文甲"),
        ])
        other, _ = statements_from([
            make_record("north", "P26-8", "same", "issued", "2026-11-01", text="原文乙"),
        ], first_capture=2)
        observations = {"statements": statements + other, "rejections": [], "probes": []}
        view = build_view(observations, business_time=datetime(2026, 11, 20,
                                                               tzinfo=timezone.utc))
        thread = north_thread(view)
        self.assertEqual(thread["status"], "conflict")
        self.assertEqual(thread["conflicts"][0]["code"], "edition_identity_conflict")
        self.assertEqual(len(thread["conflicts"][0]["statements"]), 2)

    def test_multiple_successors_is_conflict_with_all_raw_texts(self):
        records = [
            make_record("north", "P26-8", "root", "issued", "2026-11-01"),
            make_record("north", "P26-8", "succ-a", "issued", "2026-11-02",
                        replaces="root", text="后继甲"),
            make_record("north", "P26-8", "succ-b", "issued", "2026-11-03",
                        replaces="root", text="后继乙"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "conflict")
        conflict = next(item for item in thread["conflicts"]
                        if item["code"] == "multiple_successors")
        self.assertEqual({item["raw"]["text"] for item in conflict["statements"]},
                         {"后继甲", "后继乙"})

    def test_correction_inherits_predecessor_effect(self):
        records = [
            make_record("north", "P26-8", "first", "issued", "2026-11-01",
                        start="2026-11-10"),
            make_record("north", "P26-8", "fix", "corrected", "2026-11-02",
                        replaces="first", text="更正笔误，许可继续有效"),
        ]
        thread = north_thread(view_of(records))
        self.assertEqual(thread["status"], "effective")
        self.assertEqual(thread["head_edition"], "fix")
        self.assertEqual(thread["chain"][1]["raw"]["text"], "更正笔误，许可继续有效")

    def test_bitemporal_replay_ignores_later_observations(self):
        statements, rejections = statements_from(self.chain_records())
        observations = {"statements": statements, "rejections": rejections, "probes": []}
        cutoff = datetime(2026, 11, 10, 8, 0, 30, tzinfo=timezone.utc)
        view = build_view(observations, business_time=datetime(2026, 11, 20,
                                                                tzinfo=timezone.utc),
                          observed_by=cutoff)
        self.assertEqual(north_thread(view)["status"], "effective")
        early = datetime(2026, 11, 10, 7, 59, 59, tzinfo=timezone.utc)
        empty = build_view(observations, business_time=early, observed_by=early)
        self.assertEqual(empty["threads"], [])

    def test_bad_records_are_rejected_without_killing_the_batch(self):
        statements, rejections = statements_from([
            {"authority": "north", "notice_id": "P26-8", "edition": "ok",
             "kind": "issued", "published": "2026-11-01", "precision": "day"},
            {"authority": "north", "notice_id": "P26-8", "edition": "bad",
             "kind": "mystery", "published": "2026-11-01", "precision": "day"},
        ])
        self.assertEqual(len(statements), 1)
        self.assertEqual(rejections[0]["reason"], "unknown kind: mystery")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive_path = root / "raw.sqlite3"
        self.ledger_path = root / "ledger.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, origin, path, source):
        return collect(Client(origin.base_url), self.archive, source, path)[0]

    def test_snapshot_is_immutable_to_later_corrections(self):
        scenario = json.loads(Path("fixtures/origin.json").read_text(encoding="utf-8"))
        self.archive = Archive(self.archive_path)
        with serve(scenario) as origin:
            for index in range(1, 4):
                self.capture(origin, f"/observations/{index}", "north-feed")
            observations = load_observations(self.archive)
            view = build_view(observations,
                              business_time=datetime(2026, 11, 20, tzinfo=timezone.utc))
            ledger = Ledger(self.ledger_path)
            record = ledger.save(view, "2026-11-15 进场依据",
                                 sorted(probe["capture_id"] for probe in observations["probes"]))
            snapshot_id = record["snapshot_id"]
            before = ledger.get(snapshot_id)
            self.assertEqual(before["purpose"], "2026-11-15 进场依据")

            # 后续更正进入同一原始归档。
            self.capture(origin, "/observations/5", "search-cache")
            ledger.close()

            ledger = Ledger(self.ledger_path)
            after = ledger.get(snapshot_id)
            self.assertEqual(after["view"], before["view"])
            report = ledger.verify(snapshot_id, self.archive)
            self.assertTrue(report["intact"])
            self.assertEqual(report["capture_ids"], [1, 2, 3])
            ledger.close()
        self.archive.close()

    def test_snapshot_requires_evidence(self):
        ledger = Ledger(self.ledger_path)
        try:
            with self.assertRaisesRegex(ValueError, "at least one capture"):
                ledger.save({"business_time": {"value": "2026-11-20T00:00:00"}}, "x", [])
        finally:
            ledger.close()


class StagedHttpIngestionTests(unittest.TestCase):
    def test_staged_feed_reproduces_lifecycle_over_real_http(self):
        temporary = tempfile.TemporaryDirectory()
        archive = Archive(Path(temporary.name) / "raw.sqlite3")
        try:
            with serve() as origin:
                client = Client(origin.base_url)
                for index in range(1, 5):
                    collect(client, archive, "staged", f"/observations/{index}")
            view = build_view(load_observations(archive),
                              business_time=datetime(2026, 11, 20, tzinfo=timezone.utc))
            north = north_thread(view)
            self.assertEqual(north["status"], "effective")
            self.assertEqual([node["edition"] for node in north["chain"]],
                             ["first", "withdraw", "reissue"])
            south = next(item for item in view["threads"] if item["authority"] == "south")
            self.assertEqual(south["head_edition"], "s1")
            self.assertGreaterEqual(view["coverage"]["probe_count"], 4)
            self.assertTrue(view["coverage"]["open_world"])
        finally:
            archive.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
