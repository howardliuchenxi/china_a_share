"""Single-process web server entry point for local and cloud use."""

import logging
import os

import uvicorn


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APPLICATION_LOG_FORMAT = (
    "timestamp=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s"
)


def server_address() -> tuple[str, int]:
    """Read the bind address expected by local runs or Cloud Run."""
    return (
        os.getenv("APP_HOST", DEFAULT_HOST),
        int(os.getenv("PORT", str(DEFAULT_PORT))),
    )


def configure_logging() -> None:
    """Emit application decision logs through the process root handler."""
    logging.basicConfig(level=logging.INFO, format=APPLICATION_LOG_FORMAT)


def main() -> None:
    """Serve the API and built frontend on one address."""
    configure_logging()
    host, port = server_address()
    uvicorn.run(
        "china_a_share.api:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
