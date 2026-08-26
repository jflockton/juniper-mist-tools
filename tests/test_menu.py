import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import interactive_api
import requests


class FakeValidationClient:
    def __init__(self):
        self.calls = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/self":
            return {"id": "operator"}
        return []


FAKE_SETTINGS = {
    "API_URL": "https://api.eu.mist.com",
    "MIST_API_KEY": "token",
    "ORG_ID": "org-uuid",
}


def stub_settings():
    """Keep menu rendering independent of the developer's real .env."""
    return patch.object(interactive_api, "read_settings", return_value=FAKE_SETTINGS)


class MenuSafetyTests(unittest.TestCase):
    def test_read_settings_uses_current_dotenv_not_stale_process_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "API_URL=https://api.eu.mist.com\n"
                "MIST_API_KEY=changed-file-token\n"
                "ORG_ID=org-id\n",
                encoding="utf-8",
            )
            with (
                patch.object(interactive_api, "BASE_DIR", Path(temp_dir)),
                patch.dict(
                    os.environ,
                    {"MIST_API_KEY": "old-valid-process-token"},
                    clear=False,
                ),
            ):
                settings = interactive_api.read_settings()

        self.assertEqual(settings["MIST_API_KEY"], "changed-file-token")

    def test_vlan_preparation_action_is_read_only_and_has_no_danger_gate(self):
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(interactive_api, "load_sites_for_action", return_value=None),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.run_vlan_preparation_action()

        text = output.getvalue()
        self.assertIn("writes its validated networks dataset to upload_config.json", text)
        self.assertIn("Destination selection", text)
        self.assertIn("No configuration will be sent to Mist", text)
        self.assertNotIn("NON-PRODUCTION", text)

    def test_main_menu_groups_actions_by_blast_radius(self):
        with stub_settings(), redirect_stdout(StringIO()) as output:
            interactive_api.print_main_menu([{"id": "1"}])

        text = output.getvalue()
        self.assertIn("Securitas Juniper Mist API Tool", text)
        self.assertIn("1 configured site(s)", text)
        # Status line names the target so a wrong-org run is visible up front.
        self.assertIn("api.eu.mist.com", text)
        self.assertIn("org-uuid", text)
        self.assertNotIn("token", text)
        # Groups say what each action touches, not just that it is read-only.
        self.assertIn("ALL SITES", text)
        self.assertIn("ONE SWITCH", text)
        self.assertIn("CONFIGURATION", text)
        self.assertIn("read-only", text)
        self.assertIn("Find a client by MAC", text)
        self.assertIn("Export a switch's wired clients to CSV", text)
        self.assertIn("Build upload_config.json from a source switch", text)
        self.assertIn("no change", text)
        # The destructive action is a letter, unreachable by a digit mistype.
        self.assertIn("P   PUSH upload_config.json to a switch", text)
        self.assertIn("** CHANGES CONFIG **", text)
        self.assertNotIn("NON-PRODUCTION", text)
        # Setup stays reachable once the catalogue exists.
        self.assertIn("S   Setup and diagnostics", text)
        self.assertNotIn("Validate .env", text)

    def test_config_changing_action_cannot_be_reached_by_a_digit_mistype(self):
        keys = [item.key for item in interactive_api.build_main_menu()]
        self.assertIn(interactive_api.PUSH_CONFIG_KEY, keys)
        # A letter key means no adjacent digit can reach the push by mistype,
        # and adding read-only rows can never renumber it onto a digit.
        self.assertFalse(interactive_api.PUSH_CONFIG_KEY.isdigit())
        self.assertEqual(len(set(keys)), len(keys), "duplicate menu keys")
        self.assertNotIn(interactive_api.SETUP_MENU_KEY, keys)
        self.assertNotIn(interactive_api.EXIT_KEY, keys)

    def test_setup_menu_hides_device_operations_when_catalogue_is_missing(self):
        with stub_settings(), redirect_stdout(StringIO()) as output:
            interactive_api.print_setup_menu(
                "site_codes.env contains no valid site entries"
            )

        text = output.getvalue()
        self.assertIn("You do not currently have any local Mist sites configured", text)
        self.assertIn("Validate .env", text)
        self.assertIn("Download the local Mist site catalogue", text)
        self.assertIn("0   Exit", text)
        self.assertNotIn("Export every device configuration", text)
        self.assertNotIn("CONFIGURATION", text)

    def test_setup_menu_is_reachable_from_the_main_menu_and_returns(self):
        sites = [{"id": "1", "name": "Site"}]
        with (
            stub_settings(),
            patch.object(interactive_api, "configure_client"),
            patch.object(interactive_api, "get_sites", return_value=sites),
            patch.object(
                interactive_api, "validate_api_configuration", return_value=True
            ) as validate,
            patch("builtins.input", side_effect=["s", "1", "0", "0"]),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.main()

        validate.assert_called_once_with()
        text = output.getvalue()
        # Lower case is accepted, and 0 goes back rather than quitting.
        self.assertIn("Back to the main menu", text)
        self.assertIn("Exiting.", text)

    def test_main_moves_from_initial_setup_to_operational_menu(self):
        sites = [{"id": "1", "name": "Site"}]
        with (
            stub_settings(),
            patch.object(interactive_api, "configure_client"),
            patch.object(
                interactive_api,
                "get_sites",
                side_effect=[RuntimeError("catalogue missing"), sites],
            ),
            patch.object(
                interactive_api, "refresh_site_catalogue", return_value=True
            ) as refresh,
            patch("builtins.input", side_effect=["2", "0"]),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.main()

        refresh.assert_called_once_with()
        text = output.getvalue()
        self.assertIn("SETUP", text)
        self.assertIn("1 configured site(s)", text)

    def test_main_dispatches_the_first_option_to_the_mac_finder(self):
        sites = [{"id": "1", "name": "Site"}]
        with (
            stub_settings(),
            patch.object(interactive_api, "configure_client"),
            patch.object(interactive_api, "get_sites", return_value=sites),
            patch.object(interactive_api, "run_find_client_action") as finder,
            patch("builtins.input", side_effect=["1", "0"]),
            redirect_stdout(StringIO()),
        ):
            interactive_api.main()

        finder.assert_called_once_with()

    def test_operational_menu_dispatch_covers_every_row(self):
        sites = [{"id": "1", "name": "Site"}]
        for choice, target in (
            ("1", "run_find_client_action"),
            ("2", "run_lldp_export_action"),
            ("3", "run_export_all_action"),
            ("4", "run_download_config_action"),
            ("5", "run_export_clients_action"),
            ("6", "run_vlan_preparation_action"),
            ("p", "run_custom_upload_action"),
            ("P", "run_custom_upload_action"),
        ):
            with (
                stub_settings(),
                patch.object(interactive_api, "configure_client"),
                patch.object(interactive_api, "get_sites", return_value=sites),
                patch.object(interactive_api, target) as action,
                patch("builtins.input", side_effect=[choice, "0"]),
                redirect_stdout(StringIO()),
            ):
                interactive_api.main()
            action.assert_called_once_with()

    def test_invalid_selection_lists_the_keys_from_the_menu_table(self):
        sites = [{"id": "1", "name": "Site"}]
        with (
            stub_settings(),
            patch.object(interactive_api, "configure_client"),
            patch.object(interactive_api, "get_sites", return_value=sites),
            patch("builtins.input", side_effect=["9", "0"]),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.main()

        text = output.getvalue()
        self.assertIn("Invalid selection. Valid options: 1, 2, 3, 4, 5, 6, P, S, 0", text)
    def test_find_client_action_is_read_only_and_has_no_danger_gate(self):
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(
                interactive_api,
                "load_sites_for_action",
                return_value=[{"id": "1", "name": "Site"}],
            ),
            patch.object(interactive_api, "find_client_by_mac") as finder,
        ):
            interactive_api.run_find_client_action()
        finder.assert_called_once_with([{"id": "1", "name": "Site"}])

    def test_lldp_export_action_is_read_only_and_writes_csv(self):
        rows = [{
            "site": "MK", "switch_name": "coreSw", "local_port": "ge-0/0/1",
            "neighbor_system_name": "distSw", "neighbor_mac": "aabbcc",
            "neighbor_port_desc": "",
        }]
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(
                interactive_api,
                "load_sites_for_action",
                return_value=[{"id": "s1", "name": "MK"}],
            ),
            patch.object(
                interactive_api, "collect_lldp_neighbours", return_value=rows
            ),
            patch.object(
                interactive_api, "write_lldp_csv", return_value="lldp.csv"
            ) as write_csv,
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.run_lldp_export_action()
        write_csv.assert_called_once_with(rows)
        self.assertIn("LLDP neighbour", output.getvalue())

    def test_bulk_export_confirmation_uses_device_wording(self):
        sites = [{"id": str(index)} for index in range(20)]
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(interactive_api, "load_sites_for_action", return_value=sites),
            patch.object(interactive_api, "export_all_device_configs") as export_all,
            patch("builtins.input", return_value="n") as prompt,
            redirect_stdout(StringIO()),
        ):
            interactive_api.run_export_all_action()

        prompt.assert_called_once_with(
            "\nExport device configurations from all 20 sites? (Y/N): "
        )
        export_all.assert_not_called()

    def test_get_devices_only_filters_when_a_device_type_is_requested(self):
        client = MagicMock()
        client.get_json.return_value = []
        with patch.object(interactive_api, "get_client", return_value=client):
            interactive_api.get_devices("site-id")
            _, kwargs = client.get_json.call_args
            self.assertNotIn("type", kwargs["params"])

            interactive_api.get_devices("site-id", device_type="switch")
            _, kwargs = client.get_json.call_args
            self.assertEqual(kwargs["params"]["type"], "switch")

    def test_bulk_export_reports_sites_with_no_devices(self):
        with (
            patch.object(interactive_api, "get_devices", return_value=[]),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.export_all_device_configs(
                [{"id": "site-id", "name": "Gloucester"}]
            )

        text = output.getvalue()
        self.assertIn("Collecting configurations for all devices", text)
        self.assertIn("Site: Gloucester", text)
        self.assertIn("No devices found", text)

    def test_download_switch_config_is_read_only(self):
        config = {"id": "device", "name": "Switch"}
        with (
            patch.object(interactive_api, "get_device_info", return_value=config),
            patch.object(
                interactive_api, "save_device_config", return_value="output.log"
            ),
            patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
            redirect_stdout(StringIO()),
        ):
            interactive_api.download_switch_config("site", "device")

    def test_validation_checks_token_and_org_without_printing_token(self):
        settings = {
            "API_URL": "https://api.eu.mist.com",
            "MIST_API_KEY": "super-secret-token",
            "ORG_ID": "org-id",
        }
        client = FakeValidationClient()
        with (
            patch.object(interactive_api, "read_settings", return_value=settings),
            patch.object(interactive_api, "MistClient", return_value=client),
            patch.object(interactive_api, "get_sites", return_value=[{"id": "1"}]),
            patch.object(interactive_api, "_CLIENT", None),
            patch.object(interactive_api, "_SETTINGS", None),
            redirect_stdout(StringIO()) as output,
        ):
            result = interactive_api.validate_api_configuration()

        text = output.getvalue()
        self.assertTrue(result)
        self.assertIn("API token authentication: accepted", text)
        self.assertIn("Organisation access", text)
        self.assertNotIn("super-secret-token", text)
        self.assertEqual(client.calls[0][0], "/api/v1/self")

    def test_validation_stops_before_api_when_org_id_is_missing(self):
        settings = {
            "API_URL": "https://api.eu.mist.com",
            "MIST_API_KEY": "token",
            "ORG_ID": "",
        }
        with (
            patch.object(interactive_api, "read_settings", return_value=settings),
            patch.object(interactive_api, "MistClient") as mist_client,
            redirect_stdout(StringIO()),
        ):
            result = interactive_api.validate_api_configuration()

        self.assertFalse(result)
        mist_client.assert_not_called()

    def test_failed_validation_clears_previously_valid_client(self):
        settings = {
            "API_URL": "https://api.eu.mist.com",
            "MIST_API_KEY": "changed-invalid-token",
            "ORG_ID": "org-id",
        }
        failing_client = FakeValidationClient()
        failing_client.get_json = lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("authentication failed")
        )
        with (
            patch.object(interactive_api, "read_settings", return_value=settings),
            patch.object(interactive_api, "MistClient", return_value=failing_client),
            patch.object(interactive_api, "_CLIENT", object()),
            patch.object(interactive_api, "_SETTINGS", {"old": "settings"}),
            redirect_stdout(StringIO()),
        ):
            result = interactive_api.validate_api_configuration()
            self.assertIsNone(interactive_api._CLIENT)
            self.assertIsNone(interactive_api._SETTINGS)

        self.assertFalse(result)

    def test_ensure_client_reloads_dotenv_for_each_top_level_action(self):
        new_client = object()
        with (
            patch.object(interactive_api, "_CLIENT", object()),
            patch.object(
                interactive_api, "configure_client", return_value=new_client
            ) as configure,
        ):
            result = interactive_api.ensure_client()

        self.assertIs(result, new_client)
        configure.assert_called_once_with()

    def test_refresh_site_catalogue_uses_configured_org(self):
        client = object()
        sites = [{"id": "1", "name": "Site"}]
        with (
            patch.object(interactive_api, "ensure_client", return_value=client),
            patch.object(interactive_api, "_SETTINGS", {"ORG_ID": "org-id"}),
            patch.object(interactive_api, "get_all_sites", return_value=sites) as get_all,
            patch.object(interactive_api, "write_site_codes", return_value=1) as write,
            redirect_stdout(StringIO()),
        ):
            result = interactive_api.refresh_site_catalogue()

        self.assertTrue(result)
        get_all.assert_called_once_with(client, "org-id")
        write.assert_called_once_with(sites)

    def test_empty_catalogue_keeps_initial_setup_active(self):
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(interactive_api, "_SETTINGS", {"ORG_ID": "org-id"}),
            patch.object(interactive_api, "get_all_sites", return_value=[]),
            patch.object(interactive_api, "write_site_codes", return_value=0),
            redirect_stdout(StringIO()) as output,
        ):
            result = interactive_api.refresh_site_catalogue()

        self.assertFalse(result)
        self.assertIn("Initial setup is still required", output.getvalue())

    def test_custom_upload_requires_separate_danger_phrase(self):
        with (
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(interactive_api, "load_sites_for_action") as load_sites,
            patch("builtins.input", return_value="wrong phrase"),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.run_custom_upload_action()

        load_sites.assert_not_called()
        self.assertIn("CUSTOM SWITCH CONFIGURATION PUT", output.getvalue())


if __name__ == "__main__":
    unittest.main()
