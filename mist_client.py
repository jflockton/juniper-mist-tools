"""Small, shared HTTP client for the Juniper Mist REST API."""

from urllib.parse import urlsplit

import requests


class MistClient:
    """Apply consistent authentication, timeout, URL, and JSON handling."""

    def __init__(self, api_url, api_key, timeout=30, requester=None):
        self.api_url = str(api_url).strip().rstrip("/")
        self.api_key = str(api_key).strip()
        self.timeout = timeout
        self.requester = requester or requests

        parsed = urlsplit(self.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API_URL must be a complete HTTP(S) URL")
        if not self.api_key:
            raise ValueError("MIST_API_KEY must not be empty")

        self._origin = (parsed.scheme.casefold(), parsed.netloc.casefold())

    def resolve_url(self, path_or_url):
        """Resolve an API path while rejecting cursors for a different host."""
        value = str(path_or_url or "").strip()
        if not value:
            raise ValueError("Mist API path must not be empty")

        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            origin = (parsed.scheme.casefold(), parsed.netloc.casefold())
            if origin != self._origin:
                raise ValueError("Mist pagination cursor points to an unexpected host")
            return value

        if not value.startswith("/"):
            value = f"/{value}"
        return f"{self.api_url}{value}"

    def request_json(self, method, path_or_url, *, params=None, json_data=None):
        """Make an authenticated request and require a JSON response."""
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Accept": "application/json",
        }
        if json_data is not None:
            headers["Content-Type"] = "application/json"

        response = self.requester.request(
            method,
            self.resolve_url(path_or_url),
            headers=headers,
            params=params,
            json=json_data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if getattr(response, "status_code", None) == 204 or getattr(
            response, "content", None
        ) == b"":
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise RuntimeError("Mist returned a non-JSON response") from error

    def get_json(self, path_or_url, *, params=None):
        return self.request_json("GET", path_or_url, params=params)

    def put_json(self, path_or_url, payload):
        return self.request_json("PUT", path_or_url, json_data=payload)
