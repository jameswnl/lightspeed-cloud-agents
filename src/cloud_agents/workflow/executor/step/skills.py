"""Skills capability loading for spawn: none and spawn: local.

Loads skills from directories via pydantic-ai-skills SkillsCapability.
Directories are configured via CLOUD_AGENTS_SKILLS_PATHS env var
(colon-separated paths).

Trust model: skills directories contain SKILL.md files with instructions
and optional scripts that execute arbitrary Python. Only configure paths
to directories owned by trusted users. Do not point at world-writable or
user-uploaded directories.
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


def get_skills_capability(
    include: list[str] | None = None,
) -> Any | None:
    """Create a SkillsCapability from configured directories.

    Reads CLOUD_AGENTS_SKILLS_PATHS env var (colon-separated paths).
    Returns None if no paths are configured or all paths are invalid.
    When include is an empty list, returns None (no skills requested).

    Paths are resolved to absolute paths. Non-existent directories
    are skipped with a warning.

    Parameters:
        include: Optional allowlist of skill names to expose. None means
            no filtering (all skills). Passed through to
            SkillsCapability(include=...). The caller (executor)
            maps a step with no allowed_skills to no capability at all.

    Returns:
        SkillsCapability instance, or None.
    """
    # Per-step allowlist: empty list means no skills; None means no filtering (all skills).
    # Steps that did not request any skills should not call get_skills_capability at all
    # (executor handles allowed_skills is None => no skills). This keeps
    # direct calls like get_skills_capability() for tests returning all skills.
    if include is not None and len(include) == 0:
        return None

    paths_str = os.environ.get(SKILLS_PATHS_ENV, "")
    paths = [p.strip() for p in paths_str.split(":") if p.strip()]

    if not paths:
        return None

    valid_paths = []
    for p in paths:
        resolved = os.path.realpath(p)
        if os.path.isdir(resolved):
            valid_paths.append(resolved)
        else:
            logger.warning("Skills path does not exist or is not a directory: %s", p)

    if not valid_paths:
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

    logger.info("Loading skills from directories: %s (include=%s)", valid_paths, include)
    # validate=False: skip checking SKILL.md schema at construction time
    # to avoid startup failures from minor schema issues in skill files
    return skills_cls(directories=valid_paths, include=include, validate=False)
