import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import interactive_api


class FakeValidationClient:
    def __init__(self):
        self.calls = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/self":
            return {"id": "operator"}
        return []


class MenuSafetyTests(unittest.TestCase):
    def test_vlan_copy_feature_flag_is_disabled_by_default(self):
        self.assertFalse(interactive_api.vlan_copy_enabled({}))
        self.assertTrue(
            interactive_api.vlan_copy_enabled(
                {interactive_api.VLAN_COPY_ENV_FLAG: "true"}
            )
        )

    def test_vlan_copy_action_stops_before_api_when_disabled(self):
        with (
            patch.object(interactive_api, "read_settings", return_value={}),
            patch.object(interactive_api, "ensure_client") as ensure_client,
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.run_vlan_copy_action()

        ensure_client.assert_not_called()
        self.assertIn("DISABLED by default", output.getvalue())

    def test_vlan_copy_warning_requires_exact_non_production_phrase(self):
        settings = {interactive_api.VLAN_COPY_ENV_FLAG: "true"}
        with (
            patch.object(interactive_api, "read_settings", return_value=settings),
            patch.object(interactive_api, "ensure_client", return_value=object()),
            patch.object(interactive_api, "load_sites_for_action") as load_sites,
            patch("builtins.input", return_value="wrong phrase"),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.run_vlan_copy_action()

        load_sites.assert_not_called()
        text = output.getvalue()
        self.assertIn("NON-PRODUCTION VLAN COPY ONLY", text)
        self.assertIn("drop production traffic", text)

    def test_main_menu_separates_read_only_and_change_operations(self):
        with (
            patch.object(interactive_api, "read_settings", return_value={}),
            patch.object(interactive_api, "get_sites", return_value=[{"id": "1"}]),
            redirect_stdout(StringIO()) as output,
        ):
            interactive_api.print_main_menu()

        text = output.getvalue()
        self.assertIn("Welcome to the Securitas Juniper Mist API Tool", text)
        self.assertIn("Read-only operations", text)
        self.assertIn("Configuration changes", text)
        self.assertIn("NON-PRODUCTION ONLY", text)
        self.assertIn("[DANGER]", text)

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
            interactive_api.VLAN_COPY_ENV_FLAG: "",
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
            interactive_api.VLAN_COPY_ENV_FLAG: "",
        }
        with (
            patch.object(interactive_api, "read_settings", return_value=settings),
            patch.object(interactive_api, "MistClient") as mist_client,
            redirect_stdout(StringIO()),
        ):
            result = interactive_api.validate_api_configuration()

        self.assertFalse(result)
        mist_client.assert_not_called()

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
