"""Test configuration for mcp2term."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CLIENT_SRC = ROOT / "client" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(CLIENT_SRC) not in sys.path:
    sys.path.insert(0, str(CLIENT_SRC))
