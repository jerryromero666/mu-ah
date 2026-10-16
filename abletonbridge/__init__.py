"""Ableton Bridge - move Ableton Live projects between a Mac and a PC with one click.

The external drive acts as the hand-off point. Every project that goes through
Ableton Bridge is made self-contained first (all samples, Max for Live devices
and other referenced files are copied into the project folder and the set is
rewritten to use project-relative paths), so it opens on the other computer
without "missing media" errors. Plugins used by the set are checked against a
plugin inventory that each computer leaves on the drive.
"""

__version__ = "1.0.0"
APP_NAME = "Ableton Bridge"
