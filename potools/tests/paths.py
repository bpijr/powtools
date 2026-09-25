"""Put the tool folders on sys.path so tests import modules by their flat names."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in ("tests", "tools", "blender", "formats"):
    path = str(ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)
