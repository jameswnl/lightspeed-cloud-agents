"""Built-in tool: http_request -- HTTP GET/POST with safety limits.

Trust model: inputs are LLM-directed and should be treated as potentially
adversarial. URL validation, size limits, and timeout bounds are enforced.
Network-level controls (NetworkPolicy) provide defense-in-depth.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx

from cloud_agents.workflow.executor.step.tools import step_tool

_MAX_RESPONSE_BYTES = 1_048_576  # 1 MB
_MAX_TIMEOUT_SECONDS = 300
_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal"}


def _validate_url(url: str) -> str | None:
    """Validate URL for SSRF safety.

    Returns:
        Error message if URL is blocked, None if safe.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"Invalid URL: {url}"

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"Scheme '{parsed.scheme}' not allowed. Use http or https."

    hostname = parsed.hostname or ""

    if hostname in _BLOCKED_HOSTS:
        return f"Blocked host: {hostname}"

    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return f"Blocked address: {hostname} (private/loopback/link-local)"
    except ValueError:
        pass

    return None


@step_tool(
    "http_request",
    description="Make HTTP GET or POST requests with timeout and size limits",
)
def http_request(
    url: str,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> str:
    """Make an HTTP request.

    Parameters:
        url: The URL to request (http/https only, no private IPs).
        method: HTTP method -- 'GET' or 'POST' (default: 'GET').
        body: Request body for POST requests (JSON string).
        headers: Optional request headers.
        timeout_seconds: Request timeout in seconds (default: 30, max: 300).

    Returns:
        Response body as string, or error message on failure.
    """
    url_error = _validate_url(url)
    if url_error:
        return f"Error: {url_error}"

    if method.upper() not in ("GET", "POST"):
        return f"Error: method '{method}' not supported. Use GET or POST."

    timeout_seconds = max(1, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            with client.stream(method.upper(), url, content=body, headers=headers) as response:
                response.raise_for_status()

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        return f"Response too large (>{_MAX_RESPONSE_BYTES} bytes)"
                    chunks.append(chunk)

                return b"".join(chunks).decode()
    except httpx.HTTPStatusError as exc:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
    except httpx.TimeoutException:
        return f"Request timed out after {timeout_seconds}s"
    except Exception as exc:
        return f"Request failed: {exc}"
