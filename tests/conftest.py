"""Root conftest that ensures the project root is on sys.path.

Enables cross-package imports like ``from tests.load.helpers import ...``
in test files that need to reference shared test utilities.
"""

import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
