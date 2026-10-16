#!/bin/bash
# Double-click to run Ableton Bridge from source on a Mac (needs Python 3.10+ from python.org).
cd "$(dirname "$0")"
exec python3 AbletonBridge.py "$@"
