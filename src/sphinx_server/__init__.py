"""Top-level package entrypoints for :mod:`sphinx_server`.

Importing exposes :func:`sphinx_server.main.main` so the CLI script can re-use it.
"""

from .main import main

__all__ = ["main"]
