"""ASGI entry point (``uvicorn app.main:app``).

The app is built at import time on purpose, so invalid configuration stops the
server at startup. Tests and other code import ``app.factory.create_app`` instead.
"""

from app.factory import create_app

app = create_app()
