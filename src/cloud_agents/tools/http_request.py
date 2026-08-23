"""Built-in tool: http_request -- HTTP GET/POST with safety limits."""

from __future__ import annotations

import httpx

from cloud_agents.workflow.executor.step.tools import step_tool

_MAX_RESPONSE_BYTES = 1_048_576  # 1 MB


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
        url: The URL to request.
        method: HTTP method -- 'GET' or 'POST' (default: 'GET').
        body: Request body for POST requests (JSON string).
        headers: Optional request headers.
        timeout_seconds: Request timeout in seconds (default: 30).

    Returns:
        Response body as string, or error message on failure.
    """
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            if method.upper() == "POST":
                response = client.post(url, content=body, headers=headers)
            else:
                response = client.get(url, headers=headers)

            response.raise_for_status()

            if len(response.content) > _MAX_RESPONSE_BYTES:
                return (
                    f"Response too large ({len(response.content)} bytes, "
                    f"limit {_MAX_RESPONSE_BYTES})"
                )

            return response.text
    except httpx.HTTPStatusError as exc:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
    except httpx.TimeoutException:
        return f"Request timed out after {timeout_seconds}s"
    except Exception as exc:
        return f"Request failed: {exc}"
