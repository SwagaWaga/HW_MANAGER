"""Backward-compatible proxy module for app.services.sync_service."""
import sys
import os

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.services.sync_service import *
