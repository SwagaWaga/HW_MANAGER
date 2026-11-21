"""Backward-compatible entry point and proxy module for app.bot.bot."""
import sys
import os
import asyncio

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.bot.bot import *

if __name__ == "__main__":
    from app.bot.bot import main
    asyncio.run(main())
