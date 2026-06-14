"""Entry point: python -m grok_mcp (from scripts/mcp on PYTHONPATH)."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
