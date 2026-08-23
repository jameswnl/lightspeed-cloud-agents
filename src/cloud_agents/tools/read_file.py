"""Built-in tool: read_file -- read file contents with safety limits."""

from __future__ import annotations

import os

from cloud_agents.workflow.executor.step.tools import step_tool

_MAX_FILE_BYTES = 1_048_576  # 1 MB


@step_tool("read_file", description="Read file contents with size limits")
def read_file(path: str, encoding: str = "utf-8") -> str:
    """Read the contents of a file.

    Parameters:
        path: Path to the file.
        encoding: File encoding (default: 'utf-8').

    Returns:
        File contents as string, or error message on failure.
    """
    try:
        file_size = os.path.getsize(path)
        if file_size > _MAX_FILE_BYTES:
            return f"File too large ({file_size} bytes, limit {_MAX_FILE_BYTES})"

        with open(path, encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        return f"File not found: {path}"
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as exc:
        return f"Error reading file: {exc}"
