from __future__ import annotations

import uvicorn

from .config import settings

def main() -> None:
    """Main function to run sphinx documentation hosting application"""
    uvicorn.run(
        "sphinx_server.app:get_app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
