"""Puts the service root (the directory holding function_app.py) on
sys.path, since this project uses a flat layout with no __init__.py files —
pytest's default import mode wouldn't otherwise find `function_app` from
tests/unit/test_function_app.py.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
