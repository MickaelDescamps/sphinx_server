"""Command-line entrypoint for running the FastAPI application."""

from __future__ import annotations

import uvicorn

from .config import settings

def main() -> None:
    """Run uvicorn with the application factory configured."""
    uvicorn.run(
        "sphinx_server.app:get_app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
