"""Command-line entrypoint for running the FastAPI application."""

from __future__ import annotations

import logging

import uvicorn

from sphinx_server.log_utils import init_logging
from sphinx_server.config import settings

logger = logging.getLogger(__name__)


def main() -> None:
    """Run uvicorn with the application factory configured."""
    init_logging()
    logger.info("Starting sphinx-server on %s:%s", settings.host, settings.port)
    uvicorn.run(
        "sphinx_server.app:get_app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
