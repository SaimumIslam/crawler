import os
import sys
import tempfile
from pathlib import Path

# Ensure the project root is importable and data dirs point to a temp location
# BEFORE crawler.config is imported (it reads env at import time).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="crawler_test_")
os.environ["CRAWLER_DATA_DIR"] = _tmp
os.environ["CRAWLER_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["CRAWLER_EXPORT_DIR"] = str(Path(_tmp) / "exports")
os.environ["CRAWLER_SESSION_DIR"] = str(Path(_tmp) / "sessions")
