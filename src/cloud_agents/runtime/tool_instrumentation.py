"""Per-tool Prometheus metrics instrumentation.

Wraps tool functions to record call count and duration metrics.
Metric failures never disrupt tool execution.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from agents.runtime.metrics import (
    ls_agent_tool_calls_total,
    ls_agent_tool_duration_seconds,
)

logger = logging.getLogger(__name__)


def _record_call(agent_name: str, tool_name: str, status: str) -> None:
    """Safely increment the tool call counter."""
    try:
        ls_agent_tool_calls_total.labels(
            agent_name=agent_name,
            tool_name=tool_name,
            status=status,
        ).inc()
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "Failed to record tool call metric for %s/%s", agent_name, tool_name
        )


def _record_duration(agent_name: str, tool_name: str, elapsed: float) -> None:
    """Safely observe tool call duration."""
    try:
        ls_agent_tool_duration_seconds.labels(
            agent_name=agent_name,
            tool_name=tool_name,
        ).observe(elapsed)
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "Failed to record tool duration metric for %s/%s", agent_name, tool_name
        )


def instrument_tool(
    fn: Callable[..., Any],
    agent_name: str,
    tool_name: str,
) -> Callable[..., Any]:
    """Wrap a tool function with Prometheus metrics instrumentation.

    Records ls_agent_tool_calls_total (success/error) and
    ls_agent_tool_duration_seconds for each invocation. Metric failures
    are logged but never disrupt tool execution.

    Args:
        fn: The tool function to wrap.
        agent_name: Agent identifier for metric labels.
        tool_name: Tool identifier for metric labels.

    Returns:
        Wrapped function with identical signature.
    """
    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                _record_call(agent_name, tool_name, "success")
                return result
            except Exception:
                _record_call(agent_name, tool_name, "error")
                raise
            finally:
                _record_duration(agent_name, tool_name, time.monotonic() - start)

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            _record_call(agent_name, tool_name, "success")
            return result
        except Exception:
            _record_call(agent_name, tool_name, "error")
            raise
        finally:
            _record_duration(agent_name, tool_name, time.monotonic() - start)

    return sync_wrapper
