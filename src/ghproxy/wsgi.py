"""WSGI entry point for gunicorn/uwsgi."""

from .app import create_app
from .config import load_config

app = create_app(load_config([]))
