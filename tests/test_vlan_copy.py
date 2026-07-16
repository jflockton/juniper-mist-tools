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
    def test_source_metadata_rejects_a_different_upload_dataset(self):
        payload = {"networks": {"new": network(20)}}
        metadata = {
            "payload_sha256": "not-the-current-payload-hash",
            "kind": "vlan_source_dataset",
            "source": {
                "site_id": "site-a",
                "device_id": "source-id",
                "device_name": "Source",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata_path = Path(temp_dir) / "upload_config.meta.json"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                interactive_api.load_upload_metadata(payload, metadata_path)

    def test_option_three_writes_source_dataset_without_destination(self):
        source_site = {"id": "site-a", "name": "Site A"}
        source_device = {"id": "source-id", "name": "Source"}
        source_config = {
            "id": "source-id",
            "name": "Source",
            "networks": {"users": network(20), "voice": network(30)},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = Path(temp_dir) / "upload_config.json"
            metadata_path = Path(temp_dir) / "upload_config.meta.json"
            upload_path.write_text('{"old": true}', encoding="utf-8")
            with (
                patch.object(
                    interactive_api,
                    "get_device_info",
                    return_value=source_config,
                ),
                patch.object(interactive_api, "UPLOAD_CONFIG_FILE", upload_path),
                patch.object(interactive_api, "UPLOAD_METADATA_FILE", metadata_path),
                patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
                redirect_stdout(StringIO()) as output,
            ):
                interactive_api.prepare_vlan_dataset(source_site, source_device)

            payload = json.loads(upload_path.read_text(encoding="utf-8"))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(payload, {"networks": source_config["networks"]})
        self.assertEqual(metadata["kind"], "vlan_source_dataset")
        self.assertEqual(metadata["source"]["device_id"], "source-id")
        self.assertEqual(
            metadata["payload_sha256"], interactive_api._payload_sha256(payload)
        )
        self.assertIn("destination will be selected", output.getvalue().casefold())
        self.assertNotIn("PUT accepted", output.getvalue())

    def test_option_four_compares_destination_and_builds_safe_merged_payload(self):
        sites = [{"id": "site-a", "name": "Site A"}]
        source_payload = {"networks": {"new": network(20)}}
        metadata = {
            "version": 1,
            "kind": "vlan_source_dataset",
            "payload_sha256": interactive_api._payload_sha256(source_payload),
            "source": {
                "site_id": "site-a",
                "site_name": "Site A",
                "device_id": "source-id",
                "device_name": "Source",
            },
        }
        destination_device = {"id": "destination-id", "name": "Destination"}
        destination_config = {
            "name": "Destination",
            "networks": {"local": network(10)},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = Path(temp_dir) / "upload_config.json"
            metadata_path = Path(temp_dir) / "upload_config.meta.json"
            upload_path.write_text(json.dumps(source_payload), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with (
                patch.object(interactive_api, "UPLOAD_CONFIG_FILE", upload_path),
                patch.object(interactive_api, "UPLOAD_METADATA_FILE", metadata_path),
                patch.object(interactive_api, "ensure_client", return_value=object()),
                patch.object(interactive_api, "load_sites_for_action", return_value=sites),
                patch.object(
                    interactive_api,
                    "select_destination_switch",
                    return_value=(sites[0], destination_device),
                ),
                patch.object(
                    interactive_api, "get_device_info", return_value=destination_config
                ),
                patch.object(
                    interactive_api, "save_device_config", return_value="snapshot.log"
                ),
                patch.object(interactive_api, "upload_config_with_preview") as upload,
                patch("builtins.input", return_value="APPLY CUSTOM CONFIG"),
                redirect_stdout(StringIO()) as output,
            ):
                interactive_api.run_custom_upload_action()

        upload.assert_called_once_with(
            "site-a",
            "destination-id",
            destination_config,
            upload_data={
                "networks": {"local": network(10), "new": network(20)}
            },
            expected_networks={"local": network(10)},
            show_unified_diff=False,
        )
        self.assertIn("Missing / to add:     1", output.getvalue())
        self.assertIn("Networks that will be added to 'Destination'", output.getvalue())

    def test_vlan_preview_shows_only_additions_without_unified_hunks(self):
        source_networks = {"new": network(20)}
        destination_networks = {"local": network(10)}
        plan = plan_network_merge(source_networks, destination_networks)
        current_config = {
            "name": "Destination",
            "networks": destination_networks,
        }
        upload_data = {
            "networks": {"local": network(10), "new": network(20)}
        }
        with (
            patch("builtins.input", return_value="cancel"),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.print_network_merge_plan(
                plan,
                len(source_networks),
                len(destination_networks),
                destination_name="Destination",
            )
            interactive_api.upload_config_with_preview(
                "site-a",
                "destination-id",
                current_config,
                upload_data=upload_data,
                expected_networks=destination_networks,
                show_unified_diff=False,
            )

        text = output.getvalue()
        self.assertIn("Networks that will be added to 'Destination'", text)
        self.assertIn('"new"', text)
        self.assertIn("Existing destination networks included unchanged: 1", text)
        self.assertNotIn("@@", text)

    def test_vlan_upload_aborts_if_destination_changes_after_comparison(self):
        upload_data = {
            "networks": {"local": network(10), "new": network(20)}
        }
        current_config = {
            "name": "Destination",
            "networks": {"local": network(10)},
        }
        changed_config = {
            "name": "Destination",
            "networks": {"local": network(10), "someone-else": network(30)},
        }
        with (
            patch.object(interactive_api, "get_device_info", return_value=changed_config),
            patch.object(interactive_api, "save_timestamped_backup") as backup,
            patch.object(interactive_api, "get_client") as client,
            patch("builtins.input", return_value="Destination"),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.upload_config_with_preview(
                "site-a",
                "destination-id",
                current_config,
                upload_data=upload_data,
                expected_networks={"local": network(10)},
            )

        backup.assert_not_called()
        client.assert_not_called()
        self.assertIn("destination networks changed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
