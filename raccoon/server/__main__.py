"""Entry point for running the server as a module: python -m raccoon.server"""

import uvicorn

from raccoon.server.config import load_config


def main():
    """Run the Raccoon server."""
    config = load_config()

    uvicorn.run(
        "raccoon.server.app:app",
        host=config.host,
        port=config.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
