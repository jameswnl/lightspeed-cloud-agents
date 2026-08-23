"""Built-in tool: read_file -- read file contents with safety limits.

Trust model: inputs are LLM-directed and should be treated as potentially
adversarial. Path traversal is blocked by resolving to an absolute path
and checking against a configurable base directory. Symlinks are resolved
before the check.
"""

from __future__ import annotations

import os
from pathlib import Path

from cloud_agents.workflow.executor.step.tools import step_tool

_MAX_FILE_BYTES = 1_048_576  # 1 MB
_ALLOWED_BASE_DIR = os.environ.get("CLOUD_AGENTS_READ_FILE_BASE_DIR", "")


@step_tool("read_file", description="Read file contents with size and path limits")
def read_file(path: str, encoding: str = "utf-8") -> str:
    """Read the contents of a file.

    Parameters:
        path: Path to the file (must be under CLOUD_AGENTS_READ_FILE_BASE_DIR if set).
        encoding: File encoding (default: 'utf-8').

    Returns:
        File contents as string, or error message on failure.
    """
    try:
        resolved = Path(path).resolve()

        if _ALLOWED_BASE_DIR:
            base = Path(_ALLOWED_BASE_DIR).resolve()
            if not resolved.is_relative_to(base):
                return (
                    f"Error: path '{path}' is outside allowed directory "
                    f"'{_ALLOWED_BASE_DIR}'"
                )

        file_size = resolved.stat().st_size
        if file_size > _MAX_FILE_BYTES:
            return f"File too large ({file_size} bytes, limit {_MAX_FILE_BYTES})"

        return resolved.read_text(encoding=encoding)
    except FileNotFoundError:
        return f"File not found: {path}"
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as exc:
        return f"Error reading file: {exc}"
