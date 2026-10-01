"""Child process for test_transaction.py: runs a scenario and dies with
os._exit(137) at a hook label."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import txn_scenarios

kind, root, label = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
pd = root / "archive" / txn_scenarios.H


def hook(current: str) -> None:
    if current == label:
        os._exit(137)


txn_scenarios.act(kind, root, pd, hook)
