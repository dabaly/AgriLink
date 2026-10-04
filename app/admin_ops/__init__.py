"""Administrative operations and moderation."""

from flask import Blueprint

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

from app.admin_ops import routes  # noqa: E402,F401
