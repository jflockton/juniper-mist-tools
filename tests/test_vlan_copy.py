import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import interactive_api
from vlan_copy import (
    build_merged_networks,
    extract_networks,
    plan_network_merge,
    verify_network_merge,
)


def network(vlan_id, subnet=None):
    return {"vlan_id": vlan_id, "subnet": subnet, "subnet6": None}


class VlanPlanningTests(unittest.TestCase):
    def test_extract_networks_validates_and_copies(self):
        config = {"networks": {"users": network(10)}}

        result = extract_networks(config, "source")
        result["users"]["vlan_id"] = 20

        self.assertEqual(config["networks"]["users"]["vlan_id"], 10)

    def test_extract_networks_rejects_invalid_vlan_id(self):
        with self.assertRaisesRegex(ValueError, "invalid vlan_id"):
            extract_networks({"networks": {"users": {}}}, "source")

    def test_digit_string_vlan_id_compares_with_integer(self):
        source = extract_networks(
            {"networks": {"users": network("10")}}, "source"
        )
        destination = extract_networks(
            {"networks": {"other-name": network(10)}}, "destination"
        )

        plan = plan_network_merge(source, destination)

        self.assertEqual(plan.additions, {})
        self.assertEqual(plan.conflicts[0].destination_name, "other-name")

    def test_plan_adds_only_missing_name_and_vlan_id(self):
        source = {
            "existing": network(10),
            "new-network": network(20, "192.0.2.0/24"),
        }
        destination = {"existing": network(10), "local-only": network(30)}

        plan = plan_network_merge(source, destination)

        self.assertEqual(plan.additions, {"new-network": source["new-network"]})
        self.assertEqual(plan.unchanged, ("existing",))
        self.assertEqual(plan.conflicts, ())

    def test_plan_skips_same_name_with_different_settings(self):
        plan = plan_network_merge(
            {"users": network(10, "192.0.2.0/24")},
            {"users": network(10, "198.51.100.0/24")},
        )

        self.assertEqual(plan.additions, {})
        self.assertIn("different settings", plan.conflicts[0].reason)

    def test_plan_skips_vlan_id_used_under_another_name(self):
        plan = plan_network_merge(
            {"source-users": network(10)}, {"destination-users": network(10)}
        )

        self.assertEqual(plan.additions, {})
        self.assertEqual(plan.conflicts[0].destination_name, "destination-users")

    def test_plan_skips_case_insensitive_name_collision(self):
        plan = plan_network_merge(
            {"Users": network(10)}, {"users": network(20)}
        )

        self.assertEqual(plan.additions, {})
        self.assertIn("case-insensitive", plan.conflicts[0].reason)

    def test_plan_skips_duplicate_source_vlan_ids(self):
        plan = plan_network_merge(
            {"users-a": network(10), "users-b": network(10)}, {}
        )

        self.assertEqual(plan.additions, {})
        self.assertEqual(len(plan.conflicts), 2)

    def test_build_merged_networks_preserves_destination(self):
        destination = {"local": network(10)}
        additions = {"new": network(20)}

        merged = build_merged_networks(destination, additions)

        self.assertEqual(merged, {"local": network(10), "new": network(20)})
        self.assertEqual(destination, {"local": network(10)})

    def test_build_merged_networks_rejects_overwrite(self):
        with self.assertRaisesRegex(ValueError, "overwrite"):
            build_merged_networks({"users": network(10)}, {"users": network(20)})

    def test_verify_network_merge_enforces_existing_network_invariant(self):
        failures = verify_network_merge(
            {"local": network(10)},
            {"new": network(20)},
            {"local": network(99), "new": network(20), "unexpected": network(30)},
        )

        self.assertIn("existing network 'local' changed", failures)
        self.assertTrue(any("unexpected networks" in failure for failure in failures))


class VlanWorkflowTests(unittest.TestCase):
    def test_target_metadata_rejects_a_different_upload_payload(self):
        payload = {"networks": {"new": network(20)}}
        metadata = {
            "payload_sha256": "not-the-current-payload-hash",
            "target": {
                "site_id": "site-a",
                "device_id": "destination-id",
                "device_name": "Destination",
            },
            "expected_destination_networks": {},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "upload_config.target.json"
            target_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                interactive_api.load_upload_target_metadata(payload, target_path)

    def test_workflow_writes_target_bound_merged_payload_without_put(self):
        sites = [{"id": "site-a", "name": "Site A"}]
        source_device = {"id": "source-id", "name": "Source"}
        destination_device = {"id": "destination-id", "name": "Destination"}
        source_config = {
            "id": "source-id",
            "name": "Source",
            "networks": {"new": network(20)},
        }
        destination_config = {
            "id": "destination-id",
            "name": "Destination",
            "networks": {"local": network(10)},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = Path(temp_dir) / "upload_config.json"
            target_path = Path(temp_dir) / "upload_config.target.json"
            with (
                patch.object(
                    interactive_api,
                    "select_destination_switch",
                    return_value=(sites[0], destination_device),
                ),
                patch.object(
                    interactive_api,
                    "get_device_info",
                    side_effect=[source_config, destination_config, destination_config],
                ),
                patch.object(interactive_api, "UPLOAD_CONFIG_FILE", upload_path),
                patch.object(interactive_api, "UPLOAD_TARGET_FILE", target_path),
                patch("builtins.input", return_value="Destination"),
                redirect_stdout(StringIO()) as output,
            ):
                interactive_api.prepare_missing_vlans(sites, sites[0], source_device)

            payload = json.loads(upload_path.read_text(encoding="utf-8"))
            metadata = json.loads(target_path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload,
            {"networks": {"local": network(10), "new": network(20)}},
        )
        self.assertEqual(metadata["target"]["device_id"], "destination-id")
        self.assertEqual(
            metadata["payload_sha256"], interactive_api._payload_sha256(payload)
        )
        self.assertIn("will not send any configuration to Mist", output.getvalue())
        self.assertNotIn("PUT accepted", output.getvalue())

    def test_workflow_aborts_if_destination_changes_after_preview(self):
        sites = [{"id": "site-a", "name": "Site A"}]
        source_device = {"id": "source-id", "name": "Source"}
        destination_device = {"id": "destination-id", "name": "Destination"}
        source_config = {
            "name": "Source",
            "networks": {"new": network(20)},
        }
        destination_config = {
            "name": "Destination",
            "networks": {"local": network(10)},
        }
        changed_destination = {
            "name": "Destination",
            "networks": {"local": network(10), "someone-else": network(30)},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = Path(temp_dir) / "upload_config.json"
            target_path = Path(temp_dir) / "upload_config.target.json"
            with (
                patch.object(
                    interactive_api,
                    "select_destination_switch",
                    return_value=(sites[0], destination_device),
                ),
                patch.object(
                    interactive_api,
                    "get_device_info",
                    side_effect=[source_config, destination_config, changed_destination],
                ),
                patch.object(interactive_api, "UPLOAD_CONFIG_FILE", upload_path),
                patch.object(interactive_api, "UPLOAD_TARGET_FILE", target_path),
                patch("builtins.input", return_value="Destination"),
                redirect_stdout(StringIO()) as output,
            ):
                interactive_api.prepare_missing_vlans(sites, sites[0], source_device)

            self.assertFalse(upload_path.exists())
            self.assertFalse(target_path.exists())
        self.assertIn("destination networks changed", output.getvalue())

    def test_prepared_upload_aborts_if_target_changes_before_option_four(self):
        payload = {"networks": {"local": network(10), "new": network(20)}}
        metadata = {
            "version": 1,
            "payload_sha256": interactive_api._payload_sha256(payload),
            "target": {
                "site_id": "site-a",
                "site_name": "Site A",
                "device_id": "destination-id",
                "device_name": "Destination",
            },
            "expected_destination_networks": {"local": network(10)},
        }
        changed_config = {
            "name": "Destination",
            "networks": {"local": network(10), "someone-else": network(30)},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = Path(temp_dir) / "upload_config.json"
            target_path = Path(temp_dir) / "upload_config.target.json"
            upload_path.write_text(json.dumps(payload), encoding="utf-8")
            target_path.write_text(json.dumps(metadata), encoding="utf-8")
            with (
                patch.object(interactive_api, "UPLOAD_CONFIG_FILE", upload_path),
                patch.object(interactive_api, "UPLOAD_TARGET_FILE", target_path),
                patch.object(interactive_api, "ensure_client", return_value=object()),
                patch.object(
                    interactive_api,
                    "load_sites_for_action",
                    return_value=[{"id": "site-a", "name": "Site A"}],
                ),
                patch.object(
                    interactive_api, "get_device_info", return_value=changed_config
                ),
                patch.object(interactive_api, "upload_config_with_preview") as upload,
                patch("builtins.input", return_value="APPLY CUSTOM CONFIG"),
                redirect_stdout(StringIO()) as output,
            ):
                interactive_api.run_custom_upload_action()

        upload.assert_not_called()
        self.assertIn("target networks changed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
