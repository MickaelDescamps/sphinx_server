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
    ssl_args = {}
    if settings.ssl_certfile and settings.ssl_keyfile:
        ssl_args["ssl_certfile"] = settings.ssl_certfile
        ssl_args["ssl_keyfile"] = settings.ssl_keyfile
        if settings.ssl_keyfile_password:
            ssl_args["ssl_keyfile_password"] = settings.ssl_keyfile_password
        logger.info("SSL enabled with cert %s", settings.ssl_certfile)
    uvicorn.run(
        "sphinx_server.app:get_app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        factory=True,
        **ssl_args,
    )


if __name__ == "__main__":
    main()
