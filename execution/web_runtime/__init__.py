"""The guarded live-browser runtime behind ``anyuse watch`` and the web tools' browser replay.

The modules import each other by flat name; ``execution/watch_live.py`` puts this
directory on ``sys.path`` before importing them.
"""
