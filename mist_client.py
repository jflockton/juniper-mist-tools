"""Small, shared HTTP client for the Juniper Mist REST API."""

from urllib.parse import urlsplit

import requests


def describe_request_error(error, api_url=""):
    """Translate requests/urllib3 failures into concise operator guidance."""
    hostname = urlsplit(str(api_url or "")).hostname or "the configured API host"

    if isinstance(error, requests.HTTPError):
        response = error.response
        status = response.status_code if response is not None else None
        messages = {
            400: "Mist rejected the request (HTTP 400). Check the configured values.",
            401: "The API token was rejected (HTTP 401). Check MIST_API_KEY.",
            403: "Mist denied access (HTTP 403). Check token permissions and ORG_ID.",
            404: "The Mist endpoint was not found (HTTP 404). Check API_URL and ORG_ID.",
            429: "Mist rate-limited the request (HTTP 429). Wait and try again.",
        }
        if status in messages:
            return messages[status]
        if status is not None and status >= 500:
            return f"Mist is unavailable or returned a service error (HTTP {status})."
        return f"Mist API request failed (HTTP {status or 'unknown'})."

    if isinstance(error, requests.exceptions.ProxyError):
        return "Could not connect through the configured proxy. Check proxy settings."
    if isinstance(error, requests.exceptions.SSLError):
        return f"TLS validation failed for '{hostname}'. Check API_URL and certificates."
    if isinstance(error, requests.exceptions.Timeout):
        return f"Connection to '{hostname}' timed out. Check network access and API_URL."
    if isinstance(error, requests.exceptions.ConnectionError):
        detail = str(error).casefold()
        dns_markers = (
            "nameresolutionerror",
            "failed to resolve",
            "getaddrinfo failed",
            "name or service not known",
            "temporary failure in name resolution",
        )
        if any(marker in detail for marker in dns_markers):
            return (
                f"API hostname '{hostname}' could not be resolved. "
                "Check API_URL and the Mist cloud region."
            )
        if "connection refused" in detail:
            return f"Connection to '{hostname}' was refused. Check API_URL and firewall access."
        return f"Could not connect to '{hostname}'. Check API_URL, DNS, and network access."
    if isinstance(
        error,
        (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema),
    ):
        return "API_URL is not a valid HTTP(S) URL."
    return "Mist API request failed."


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
