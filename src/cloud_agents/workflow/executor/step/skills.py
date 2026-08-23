"""Skills capability loading for spawn: none and spawn: local.

Loads skills from directories via pydantic-ai-skills SkillsCapability.
Directories are configured via CLOUD_AGENTS_SKILLS_PATHS env var
(colon-separated paths).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SKILLS_PATHS_ENV = "CLOUD_AGENTS_SKILLS_PATHS"


def _import_skills_capability() -> type:
    """Import and return the SkillsCapability class.

    Separated for testability -- allows mocking ImportError.

    Returns:
        The SkillsCapability class.

    Raises:
        ImportError: If pydantic-ai-skills is not installed.
    """
    from pydantic_ai_skills import SkillsCapability

    return SkillsCapability


def get_skills_capability() -> Any | None:
    """Create a SkillsCapability from configured directories.

    Reads CLOUD_AGENTS_SKILLS_PATHS env var (colon-separated paths).
    Returns None if no paths are configured.

    Returns:
        SkillsCapability instance, or None.
    """
    paths_str = os.environ.get(SKILLS_PATHS_ENV, "")
    paths = [p.strip() for p in paths_str.split(":") if p.strip()]

    if not paths:
        return None

    try:
        skills_cls = _import_skills_capability()
    except ImportError:
        logger.warning(
            "pydantic-ai-skills not installed; ignoring %s=%s. "
            "Install with: pip install pydantic-ai-skills",
            SKILLS_PATHS_ENV,
            paths_str,
        )
        return None

    logger.info("Loading skills from directories: %s", paths)
    return skills_cls(directories=paths, validate=False)
