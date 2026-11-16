import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from permit_calendar.archive import Archive
from permit_calendar.client import Client, collect
from permit_calendar.derived import DerivedStore
from permit_calendar.lifecycle import judge, statement_from_record
from permit_calendar.origin import load_scenario, serve

FIXTURES = Path(__file__).parents[1] / "fixtures"


def make_statement(capture_id, item_index=0, edition="e1", kind="issued", replaces=None,
                   published="2026-11-01", precision="day", authority="north",
                   notice_id="P26-8", **extra):
    payload = {"authority": authority, "notice_id": notice_id, "edition": edition,
               "published": published, "precision": precision, **extra}
    if kind is not None:
        payload["kind"] = kind
    if replaces is not None:
        payload["replaces"] = replaces
    return statement_from_record(payload, capture_id, item_index, "2026-11-16T00:00:00.000Z")


class JudgeUnitTests(unittest.TestCase):
    def test_crawl_order_never_changes_outcome(self):
        first = make_statement(1, edition="first")
        withdraw = make_statement(2, edition="withdraw", kind="withdrawn", replaces="first",
                                  published="2026-11-03")
        reissue = make_statement(3, edition="reissue", replaces="withdraw",
                                 published="2026-11-03", start="2026-11-17")
        baseline = judge("north", "P26-8", [first, withdraw, reissue])
        for order in ([reissue, withdraw, first], [withdraw, reissue, first], [first, reissue, withdraw]):
            with self.subTest(order=[s["edition"] for s in order]):
                result = judge("north", "P26-8", order)
                self.assertEqual(result["state"], baseline["state"])
                self.assertEqual(result["head"], baseline["head"])
                self.assertEqual([n["edition"] for n in result["chain"]], ["first", "withdraw", "reissue"])

    def test_same_day_unreferenced_pair_stays_pending(self):
        one = make_statement(1, edition="a", published="2026-11-03", precision="day")
        two = make_statement(2, edition="b", published="2026-11-03", precision="day")
        result = judge("north", "P26-8", [one, two])
        self.assertEqual(result["state"], "pending")
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["type"], "competing_heads")
        self.assertEqual(sorted(conflict["heads"]), ["a", "b"])

    def test_unreferenced_pair_with_different_dates_still_pending_with_hint(self):
        older = make_statement(1, edition="a", published="2026-11-01")
        newer = make_statement(2, edition="b", published="2026-11-05")
        result = judge("north", "P26-8", [older, newer])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["hints"][0]["type"], "declared_dates_differ")

    def test_content_drift_on_head_is_blocking(self):
        issued = make_statement(1, edition="e1", kind="issued")
        rewritten = make_statement(2, edition="e1", kind="withdrawn")
        result = judge("north", "P26-8", [issued, rewritten])
        self.assertEqual(result["state"], "pending")
        drift = result["conflicts"][0]
        self.assertEqual(drift["type"], "content_drift")
        self.assertEqual(drift["severity"], "blocking")
        self.assertEqual(len(drift["statements"]), 2)

    def test_dangling_reference_is_notice_not_blocking(self):
        reissue = make_statement(1, edition="reissue", replaces="ghost")
        result = judge("north", "P26-8", [reissue])
        self.assertEqual(result["state"], "effective")
        self.assertEqual(result["conflicts"][0]["type"], "dangling_reference")
        self.assertEqual(result["conflicts"][0]["severity"], "notice")

    def test_unrecognized_kind_is_not_guessed(self):
        strange = make_statement(1, edition="e1", kind="archived")
        result = judge("north", "P26-8", [strange])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["conflicts"][0]["type"], "unrecognized_kind")

    def test_reference_cycle_is_blocking(self):
        one = make_statement(1, edition="a", replaces="b")
        two = make_statement(2, edition="b", replaces="a")
        result = judge("north", "P26-8", [one, two])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["conflicts"][0]["type"], "reference_cycle")

    def test_precision_and_published_are_preserved_verbatim(self):
        statement = make_statement(1, edition="e1", published="2026-11-01", precision="day")
        result = judge("north", "P26-8", [statement])
        self.assertEqual(result["head"]["published"], "2026-11-01")
        self.assertEqual(result["head"]["precision"], "day")

    def test_identity_fields_are_required(self):
        for field in ("authority", "notice_id", "edition"):
            payload = {"authority": "north", "notice_id": "P26-8", "edition": "e1"}
            del payload[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                statement_from_record(payload, 1, 0, "2026-11-16T00:00:00.000Z")

    def test_unobserved_group_reports_absent_enumerations(self):
        result = judge("north", "P26-8", [], enumerations=[{"capture_id": 3, "editions": set()}])
        self.assertEqual(result["state"], "unobserved")
        self.assertEqual(result["coverage"]["replacement"], "absent_as_of")


class BusinessCaseTests(unittest.TestCase):
    """逐条执行 fixtures/business_cases.json 中的业务交错情形。"""

    def run_case(self, case):
        scenario = load_scenario(FIXTURES / case["scenario"])
        with tempfile.TemporaryDirectory() as temporary, serve(scenario) as origin:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            derived = DerivedStore(Path(temporary) / "derived.sqlite3")
            client = Client(origin.base_url)
            saved = {}
            try:
                for step in case["steps"]:
                    if "capture" in step:
                        capture_id, _ = collect(client, archive, "case", step["capture"],
                                                headers=step.get("headers"))
                        if step.get("scope"):
                            derived.set_scope(capture_id, step["scope"])
                        derived.incorporate(archive)
                    elif "status" in step:
                        query = step["status"]
                        cutoff = derived.resolve_cutoff(archive, step.get("as_of_capture"),
                                                        step.get("as_of_time"))
                        judgment = derived.judgment(archive, query["authority"], query["notice_id"],
                                                    cutoff=cutoff, with_originals=True)
                        self.assert_expectations(judgment, step["expect"])
                    elif "snapshot" in step:
                        query = step["snapshot"]
                        saved[step["save_as"]] = derived.save_snapshot(
                            archive, query["authority"], query["notice_id"], query["purpose"])
                    elif "snapshot_show" in step:
                        record = derived.snapshot(saved[step["snapshot_show"]])
                        self.assert_expectations(record["judgment"], step["expect"])
                    elif "snapshot_verify" in step:
                        result = derived.verify_snapshot(archive, saved[step["snapshot_verify"]])
                        self.assertEqual(result["intact"], step["expect"]["intact"])
                    else:
                        self.fail("unknown step: %r" % (step,))
            finally:
                archive.close()
                derived.close()

    def assert_expectations(self, judgment, expect):
        if "state" in expect:
            self.assertEqual(judgment["state"], expect["state"])
        if "head" in expect:
            self.assertIsNotNone(judgment["head"])
            self.assertEqual(judgment["head"]["edition"], expect["head"])
        if "chain" in expect:
            self.assertEqual([n["edition"] for n in judgment["chain"]], expect["chain"])
        if "coverage" in expect:
            self.assertEqual(judgment["coverage"]["replacement"], expect["coverage"])
        if "horizon" in expect:
            self.assertEqual(judgment["coverage"]["horizon_capture"], expect["horizon"])
        if "conflicts" in expect:
            types = [c["type"] for c in judgment["conflicts"]]
            for expected in expect["conflicts"]:
                self.assertIn(expected, types)
        if "heads" in expect:
            conflict = next(c for c in judgment["conflicts"] if c["type"] == "competing_heads")
            self.assertEqual(sorted(conflict["heads"]), expect["heads"])

    def test_all_business_cases(self):
        cases = json.loads((FIXTURES / "business_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 6)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.run_case(case)


class DerivedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive = Archive(root / "raw.sqlite3")
        self.derived = DerivedStore(root / "derived.sqlite3")
        self.scenario = load_scenario(FIXTURES / "lifecycle.json")
        self.origin_context = serve(self.scenario)
        self.origin = self.origin_context.__enter__()
        self.client = Client(self.origin.base_url)

    def tearDown(self):
        self.origin_context.__exit__(None, None, None)
        self.archive.close()
        self.derived.close()
        self.temporary.cleanup()

    def observe(self, path, scope=None, headers=None):
        capture_id, _ = collect(self.client, self.archive, "test", path, headers=headers)
        if scope:
            self.derived.set_scope(capture_id, scope)
        self.derived.incorporate(self.archive)
        return capture_id

    def judgment(self, authority="north", notice_id="P26-8", **kwargs):
        return self.derived.judgment(self.archive, authority, notice_id, **kwargs)

    def test_incorporate_is_idempotent(self):
        self.observe("/stage/1", scope="enumeration")
        again = self.derived.incorporate(self.archive)
        self.assertEqual(again["incorporated"], 0)
        count = self.derived.connection.execute("SELECT count(*) FROM statements").fetchone()[0]
        self.assertEqual(count, 1)

    def test_late_cached_page_only_adds_a_sighting(self):
        self.observe("/stage/1", scope="enumeration")
        self.observe("/stage/2", scope="enumeration")
        self.observe("/cache/first")
        judgment = self.judgment()
        self.assertEqual(judgment["state"], "withdrawn")
        first = next(n for n in judgment["chain"] if n["edition"] == "first")
        # 两次完整列表各见一次，晚到的缓存旧页只再添一次观察，不改动头部。
        self.assertEqual(len(first["sightings"]), 3)

    def test_pending_judgment_exposes_both_originals(self):
        for stage in ("/stage/1", "/stage/2", "/stage/3", "/stage/4"):
            self.observe(stage, scope="enumeration")
        judgment = self.judgment(with_originals=True)
        self.assertEqual(judgment["state"], "pending")
        editions = {o["payload"]["edition"] for o in judgment["originals"]}
        self.assertIn("reissue", editions)
        self.assertIn("reissue-b", editions)
        starts = {o["payload"]["edition"]: o["payload"].get("start") for o in judgment["originals"]}
        self.assertEqual(starts["reissue"], "2026-11-17")
        self.assertEqual(starts["reissue-b"], "2026-11-20")

    def test_snapshots_reject_update_and_delete(self):
        self.observe("/stage/1", scope="enumeration")
        snapshot_id = self.derived.save_snapshot(self.archive, "north", "P26-8", "进场依据")
        with self.assertRaises(sqlite3.IntegrityError):
            self.derived.connection.execute("UPDATE snapshots SET purpose='改写' WHERE id=?", (snapshot_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.derived.connection.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))

    def test_as_of_time_resolves_to_knowledge_cutoff(self):
        first_capture = self.observe("/stage/1", scope="enumeration")
        self.observe("/stage/2", scope="enumeration")
        received_at = next(c["received_at"] for c in self.archive.captures() if c["id"] == first_capture)
        cutoff = self.derived.resolve_cutoff(self.archive, as_of_time=received_at)
        self.assertEqual(cutoff, first_capture)
        judgment = self.judgment(cutoff=cutoff)
        self.assertEqual((judgment["state"], judgment["head"]["edition"]), ("effective", "first"))
        ancient = self.derived.resolve_cutoff(self.archive, as_of_time="2000-01-01T00:00:00Z")
        self.assertEqual(ancient, 0)
        self.assertEqual(self.judgment(cutoff=ancient)["state"], "unobserved")

    def test_non_success_and_undecodable_captures_are_noted(self):
        self.archive.append("test", "/rate-limited", 429, {"retry-after": "2"}, b'{"error":"quota"}')
        self.archive.append("test", "/broken", 200, {"content-type": "application/json"}, b"not json")
        summary = self.derived.incorporate(self.archive)
        self.assertEqual(summary["notes"][1], "status_429")
        self.assertTrue(summary["notes"][2].startswith("decode_error"))
        self.assertEqual(summary["statements"], 0)

    def test_records_missing_identity_are_skipped_not_invented(self):
        body = json.dumps({"items": [{"authority": "north", "notice_id": "P26-8"}]}).encode()
        self.archive.append("test", "/feed", 200, {"content-type": "application/json"}, body)
        summary = self.derived.incorporate(self.archive)
        self.assertEqual(summary["statements"], 0)
        self.assertIn("edition", summary["notes"][1])

    def test_known_groups_keep_authorities_separate(self):
        self.observe("/stage/2", scope="enumeration")
        self.observe("/south/feed")
        self.assertEqual(self.derived.known_groups(), [("north", "P26-8"), ("south", "P26-8")])


if __name__ == "__main__":
    unittest.main()
