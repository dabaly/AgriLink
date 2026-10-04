"""Disputes blueprint package."""

from flask import Blueprint

disputes_bp = Blueprint("disputes", __name__)

from app.disputes import routes  # noqa: E402,F401
