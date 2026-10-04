"""Marketplace routes and services."""

from flask import Blueprint

marketplace_bp = Blueprint("marketplace", __name__)

from app.marketplace import routes  # noqa: E402,F401
