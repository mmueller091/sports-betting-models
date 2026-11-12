"""Allow cloud jobs to keep model history on a persistent state branch."""
import os
from pathlib import Path

STORE_DIR = Path(os.getenv("SPORTS_DATA_DIR", str(Path(__file__).parent.parent / "data_store")))
