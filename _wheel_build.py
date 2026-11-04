"""Build hook: the wheel ships no test files.

The repository root IS the ``anyuse`` package, so every root ``test_*.py`` was
a package module and went into the wheel. setuptools has no pattern exclusion
for the modules of a listed package (``exclude-package-data`` covers data
files only); this ``build_py`` drops them when the wheel is built.
``pyproject.toml`` names it under ``[tool.setuptools.cmdclass]``. An editable
install (``pip install -e .``) maps the checkout itself and is unaffected.
"""
from __future__ import annotations

import fnmatch
import os

from setuptools.command.build_py import build_py as _build_py

#: module file names that never ship
EXCLUDE = ("test_*.py", "conftest.py", "_wheel_build.py")


def shipped(path: str) -> bool:
    name = os.path.basename(path)
    return not any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE)


class BuildPy(_build_py):
    def find_package_modules(self, package, package_dir):
        return [m for m in super().find_package_modules(package, package_dir) if shipped(m[2])]
