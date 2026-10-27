"""Retrain both deep networks from fresh synthetic data.

    python scripts/train.py          (about one minute)
    python scripts/train.py --quick  (a few seconds, lower accuracy)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netsage.models import train_all  # noqa: E402

if __name__ == "__main__":
    m = train_all(quick="--quick" in sys.argv)
    print(f"Done in {m['train_seconds']} s. Models saved to the models/ folder.")
