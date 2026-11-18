#!/usr/bin/env python3
"""Print the reliability checklist rows that a diff hits (B209).

  python3 scripts/checklist.py                      the working tree against origin/main
  python3 scripts/checklist.py --head BRANCH        the commits origin/main...BRANCH
  python3 scripts/checklist.py --file docs/reliability-checklist.md --validate

The code is in server/reliability_checklist.py, so that the server can use it too. See its help for all options.
Exit codes: 0 no row hit, 1 rows hit, 2 not checked.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import reliability_checklist  # noqa: E402  (server/reliability_checklist.py)

if __name__ == '__main__':
    sys.exit(reliability_checklist.main())
