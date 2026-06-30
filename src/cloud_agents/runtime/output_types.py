"""Output type registry with importlib fallback.

Resolves output type class names to Python types. Built-in types
are checked first, then falls back to importlib if a module is specified.
"""

from __future__ import annotations

import importlib
import logging

from agents.models import DiagnosticReport, MonitoringResult

logger = logging.getLogger(__name__)

OUTPUT_TYPE_REGISTRY: dict[str, type] = {
    "DiagnosticReport": DiagnosticReport,
    "MonitoringResult": MonitoringResult,
    "str": str,
}


def resolve_output_type(name: str, module_name: str | None = None) -> type:
    """Resolve output type by name.

    Built-in registry is checked first. If the name is not found and
    module_name is provided, attempts to load via importlib.

    Args:
        name: Class name of the output type.
        module_name: Optional Python module to search for custom types.

    Returns:
        The resolved type class.

    Raises:
        ValueError: If the type cannot be resolved.
    """
    if name in OUTPUT_TYPE_REGISTRY:
        return OUTPUT_TYPE_REGISTRY[name]

    if module_name:
        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, name, None)
            if cls is not None and isinstance(cls, type):
                return cls
        except ImportError:
            logger.warning(
                "Could not import module '%s' for output type '%s'", module_name, name
            )

    raise ValueError(
        f"Unknown output_type '{name}'. "
        f"Provide output_type_module for custom types. "
        f"Built-in types: {list(OUTPUT_TYPE_REGISTRY)}"
    )
