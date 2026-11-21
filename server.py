"""Backward-compatible proxy module for app.api.server."""
import sys
import os

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.api.server import *
from app.api.server import app
