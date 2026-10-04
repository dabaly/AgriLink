"""Chat and offer web and realtime interfaces."""

from flask import Blueprint

chat_bp = Blueprint("chat", __name__)

from app.chat import routes, sockets  # noqa: E402,F401
