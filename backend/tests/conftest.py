"""Pytest configuration for Axiom backend tests.

Ensures the backend package is importable regardless of the working
directory pytest is invoked from.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
