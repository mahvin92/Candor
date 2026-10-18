import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # mock_engine package
sys.path.insert(0, str(ROOT / "app"))    # candor_tools, system_prompt, demo_core
os.environ.setdefault("CANDOR_ENGINE_MODE", "mock")
os.environ.setdefault("MAX_TABLE_ROWS", "500")
