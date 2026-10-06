"""Stable Streamlit entry point for the packaged application."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

APPLICATION_MODULE = "application.dashboard"
if APPLICATION_MODULE in sys.modules:
    importlib.reload(sys.modules[APPLICATION_MODULE])
else:
    importlib.import_module(APPLICATION_MODULE)
