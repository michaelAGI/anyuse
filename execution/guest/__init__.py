"""The guest side of the desktop path: runs INSIDE a macOS guest VM, never on the host.

``desktop_e2e_job.py serve`` is the job agent that compiled desktop tools talk to over
the file channel (``execution/desktop_tools.py``). The modules import each other by
flat name; each puts its own directory on ``sys.path``.
"""
