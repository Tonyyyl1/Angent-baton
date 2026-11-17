#!/usr/bin/env python3
"""relay-desk entry point: put this folder on sys.path and run relaydesk.cli."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from relaydesk.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
