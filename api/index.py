import sys
from pathlib import Path

# Add project root to sys.path so app can be imported
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import app
