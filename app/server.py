"""ASGI entry point: uv run uvicorn app.server:app --port 8000"""
import logging

from .api.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
