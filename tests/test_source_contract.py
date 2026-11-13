import hashlib
import json
import unittest

from permit_calendar.client import Client
from permit_calendar.decoding import parse_page, records as decode_records
from permit_calendar.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertEqual(records[2]["replaces"], records[1]["edition"])
            self.assertEqual(records[1]["published"], records[2]["published"])
            self.assertNotEqual(records[0]["authority"], records[3]["authority"])
            self.assertEqual(records[0]["notice_id"], records[3]["notice_id"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
