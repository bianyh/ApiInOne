import os

from relay import create_app
from relay.settings import DEFAULT_MAX_REQUEST_BYTES


app = create_app()


if __name__ == "__main__":
    from waitress import serve

    limit_mb = int(os.getenv("RELAY_MAX_BODY_MB", str(DEFAULT_MAX_REQUEST_BYTES // 1024**2)))
    if limit_mb < 1:
        raise ValueError("RELAY_MAX_BODY_MB must be at least 1")
    threads = int(os.getenv("RELAY_THREADS", "6"))
    if threads < 1:
        raise ValueError("RELAY_THREADS must be at least 1")
    app.config["MAX_CONTENT_LENGTH"] = limit_mb * 1024**2
    serve(
        app,
        host=os.getenv("RELAY_BIND", "0.0.0.0"),
        port=int(os.getenv("RELAY_PORT", "8000")),
        threads=threads,
        connection_limit=64,
        channel_timeout=1800,
        max_request_header_size=64 * 1024,
        max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
        outbuf_high_watermark=2 * 1024**2,
    )
