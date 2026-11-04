"""anyuse context package — macOS perception (screen / accessibility / app).

The Phase-3 perception layer the execution engine reads (screenshots for the VLM,
the accessibility tree for precise actions, frontmost-app/window context). Imports
are intentionally LAZY (each submodule pulls macOS frameworks via pyobjc) so
importing this package is cheap and does not require the frameworks until a
submodule is actually used.
"""
