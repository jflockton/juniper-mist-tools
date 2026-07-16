import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import interactive_api


class SequenceClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        return next(self.responses)


class InteractiveApiTests(unittest.TestCase):
    def test_import_does_not_require_runtime_settings(self):
        self.assertIsNone(interactive_api._CLIENT)

    def test_normalise_mac_removes_common_separators(self):
        self.assertEqual(
            interactive_api._normalise_mac("D4:99:6C-AA"), "d4996caa"
        )

    def test_wired_client_mapping_handles_missing_optional_fields(self):
        row = interactive_api.wired_client_to_row(
            {"mac": "aabbcc", "last_port_id": "ge-1/0/2"}, "Switch A"
        )

        self.assertEqual(row["client_mac"], "aabbcc")
        self.assertEqual(row["switch_name"], "Switch A")
        self.assertEqual(row["ip"], "")
        self.assertEqual(row["last_seen_utc"], "")

    def test_wired_client_mapping_accepts_epoch_string(self):
        row = interactive_api.wired_client_to_row(
            {"timestamp": "0", "ip": ["192.0.2.5"], "hostname": ["host"]},
            "Switch A",
        )

        self.assertEqual(row["last_seen_utc"], "1970-01-01 00:00:00")
        self.assertEqual(row["ip"], "192.0.2.5")
        self.assertEqual(row["dhcp_hostname"], "host")

    def test_get_wired_clients_follows_cursor(self):
        client = SequenceClient(
            [
                {
                    "results": [{"mac": "one"}],
                    "next": "/api/v1/sites/s/wired_clients/search?cursor=abc",
                },
                {"results": [{"mac": "two"}]},
            ]
        )
        with patch.object(interactive_api, "_CLIENT", client):
            results = interactive_api.get_wired_clients("s", 1, 2)

        self.assertEqual([item["mac"] for item in results], ["one", "two"])
        self.assertIsNone(client.calls[1][1])

    def test_get_wired_clients_rejects_repeated_cursor(self):
        cursor = "/api/v1/sites/s/wired_clients/search?cursor=abc"
        client = SequenceClient(
            [
                {"results": [], "next": cursor},
                {"results": [], "next": cursor},
            ]
        )
        with patch.object(interactive_api, "_CLIENT", client):
            with self.assertRaisesRegex(RuntimeError, "repeated"):
                interactive_api.get_wired_clients("s", 1, 2)

    def test_wired_client_rows_filter_selected_switch_mac(self):
        clients = [
            {"mac": "client-one", "last_device_mac": "D4:99:6C-AA"},
            {"mac": "client-two", "last_device_mac": "00:11:22:33"},
        ]

        rows = interactive_api.wired_client_rows_for_device(
            clients, "d4996caa", "Switch A"
        )

        self.assertEqual([row["client_mac"] for row in rows], ["client-one"])

    def test_csv_writer_uses_header_and_collision_suffix(self):
        row = {
            "client_mac": "aabbcc",
            "ip": "192.0.2.5",
            "switch_name": "Switch A",
            "port_id": "ge-0/0/1",
            "vlan": 10,
            "manufacture": "Example",
            "dhcp_hostname": "host",
            "last_seen_utc": "2026-07-16 12:00:00",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch.object(interactive_api, "OUTPUT_DIR", output_dir):
                first = Path(
                    interactive_api.write_wired_clients_csv("Site", "Switch", [row])
                )
                second = Path(
                    interactive_api.write_wired_clients_csv("Site", "Switch", [row])
                )
            with first.open(newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))

        self.assertNotEqual(first.name, second.name)
        self.assertTrue(second.stem.endswith("_2"))
        self.assertEqual(written[0]["client_mac"], "aabbcc")

    def test_build_config_diff_limits_preview_to_payload_fields(self):
        diff = interactive_api.build_config_diff(
            {"name": "Switch", "networks": {"old": 1}, "secret": "hidden"},
            {"networks": {"new": 2}},
        )

        self.assertIn('"old": 1', diff)
        self.assertIn('"new": 2', diff)
        self.assertNotIn("secret", diff)

    def test_load_upload_payload_requires_nonempty_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "upload.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not an object"):
                interactive_api.load_upload_payload(path)
            path.write_text(json.dumps({"networks": {}}), encoding="utf-8")
            self.assertEqual(
                interactive_api.load_upload_payload(path), {"networks": {}}
            )


if __name__ == "__main__":
    unittest.main()
