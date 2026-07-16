import unittest

import requests

from mist_client import MistClient, describe_request_error


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b"json"):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeRequester:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(self.payload)


class MistClientTests(unittest.TestCase):
    def test_dns_error_is_concise_and_actionable(self):
        error = requests.ConnectionError(
            "HTTPSConnectionPool: NameResolutionError: Failed to resolve host"
        )

        message = describe_request_error(
            error, "https://aaaaaaapi.eu.mist.com"
        )

        self.assertIn("'aaaaaaapi.eu.mist.com' could not be resolved", message)
        self.assertIn("Check API_URL", message)
        self.assertNotIn("HTTPSConnectionPool", message)

    def test_http_401_points_to_api_key(self):
        error = requests.HTTPError("raw response")
        error.response = type("Response", (), {"status_code": 401})()

        message = describe_request_error(error, "https://api.eu.mist.com")

        self.assertEqual(
            message, "The API token was rejected (HTTP 401). Check MIST_API_KEY."
        )

    def test_timeout_points_to_host_and_network(self):
        message = describe_request_error(
            requests.Timeout("raw timeout"), "https://api.eu.mist.com"
        )

        self.assertIn("'api.eu.mist.com' timed out", message)
        self.assertNotIn("raw timeout", message)

    def test_request_applies_authentication_and_timeout(self):
        requester = FakeRequester({"ok": True})
        client = MistClient(
            "https://api.eu.mist.com/", "secret", timeout=12, requester=requester
        )

        result = client.get_json("/api/v1/self")

        self.assertEqual(result, {"ok": True})
        args, kwargs = requester.calls[0]
        self.assertEqual(args[:2], ("GET", "https://api.eu.mist.com/api/v1/self"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Token secret")
        self.assertEqual(kwargs["timeout"], 12)

    def test_rejects_pagination_cursor_for_another_host(self):
        client = MistClient("https://api.eu.mist.com", "secret")

        with self.assertRaisesRegex(ValueError, "unexpected host"):
            client.resolve_url("https://example.invalid/api/v1/sites")

    def test_accepts_absolute_cursor_for_configured_host(self):
        client = MistClient("https://api.eu.mist.com", "secret")
        cursor = "https://api.eu.mist.com/api/v1/search?cursor=abc"

        self.assertEqual(client.resolve_url(cursor), cursor)

    def test_empty_success_response_is_valid_for_put(self):
        class EmptyRequester:
            def request(self, *args, **kwargs):
                return FakeResponse(None, status_code=204, content=b"")

        client = MistClient(
            "https://api.eu.mist.com", "secret", requester=EmptyRequester()
        )

        self.assertEqual(client.put_json("/api/v1/device", {"networks": {}}), {})


if __name__ == "__main__":
    unittest.main()
