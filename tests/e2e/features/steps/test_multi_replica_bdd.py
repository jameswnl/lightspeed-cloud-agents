"""pytest-bdd runner for multi-replica E2E scenarios.

Imports step definitions from multi_replica_steps.py and links them
to the Gherkin feature file. All steps are imported implicitly via
the scenarios() call in multi_replica_steps.

Prerequisites:
  - Kind cluster with 2-replica workflow runner deployed
  - kubectl configured for the Kind cluster

Usage:
  uv run pytest tests/e2e/features/steps/test_multi_replica_bdd.py -v

Skip when no cluster:
  Tests auto-skip when kubectl is unavailable or cluster not found.
"""

from tests.e2e.features.steps.multi_replica_steps import *  # noqa: F401, F403
