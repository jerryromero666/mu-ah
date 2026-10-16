"""Launcher used by PyInstaller and the run scripts."""
import sys

from abletonbridge.cli import main

if __name__ == "__main__":
    sys.exit(main())
