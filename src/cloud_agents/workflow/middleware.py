"""ASGI middleware for request body size enforcement.

Provides ContentSizeLimitMiddleware which rejects HTTP requests whose
body exceeds a configurable byte threshold, preventing memory exhaustion
and downstream payload-limit errors (e.g., Temporal's 4 MB gRPC cap).
"""

from __future__ import annotations

from starlette.responses import JSONResponse


class ContentSizeLimitMiddleware:
    """ASGI middleware that rejects requests exceeding a body size limit.

    Handles both Content-Length header and chunked transfer encoding
    by counting bytes from receive() during streaming.

    Attributes:
        app: The wrapped ASGI application.
        max_content_size: Maximum allowed request body size in bytes.
    """

    def __init__(self, app, max_content_size: int = 1_048_576) -> None:
        """Initialize the middleware.

        Parameters:
            app: The ASGI application to wrap.
            max_content_size: Maximum allowed request body size in bytes.
                Defaults to 1 MB (1,048,576 bytes).
        """
        self.app = app
        self.max_content_size = max_content_size

    async def __call__(self, scope, receive, send) -> None:
        """Process an ASGI request, enforcing body size limits.

        For HTTP requests, checks the Content-Length header first (fast
        rejection) and then counts bytes from receive() to catch chunked
        transfer encoding.

        Non-HTTP scopes (WebSocket, lifespan) pass through unchanged.

        Parameters:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: check Content-Length header if present
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if int(value) > self.max_content_size:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"Request body too large. "
                                f"Maximum: {self.max_content_size} bytes."
                            )
                        },
                    )
                    await response(scope, receive, send)
                    return
                break

        # Slow path: count bytes from receive() for chunked encoding
        total_bytes = 0

        async def size_limited_receive():
            """Wrap receive() to count bytes and enforce the limit."""
            nonlocal total_bytes
            message = await receive()
            if message.get("type") == "http.request":
                total_bytes += len(message.get("body", b""))
                if total_bytes > self.max_content_size:
                    raise _BodyTooLargeError(self.max_content_size)
            return message

        try:
            await self.app(scope, size_limited_receive, send)
        except _BodyTooLargeError:
            response = JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Request body too large. "
                        f"Maximum: {self.max_content_size} bytes."
                    )
                },
            )
            await response(scope, receive, send)


class _BodyTooLargeError(Exception):
    """Internal signal that the request body exceeded the size limit."""

    def __init__(self, max_size: int) -> None:
        """Initialize with the configured maximum size.

        Parameters:
            max_size: The configured maximum body size in bytes.
        """
        super().__init__(f"Request body exceeds {max_size} bytes")
        self.max_size = max_size
