"""Authenticated Socket.IO events; every mutation delegates to services."""

from flask import current_app, request
from flask_login import current_user
from flask_socketio import join_room
from flask_wtf.csrf import validate_csrf
from werkzeug.exceptions import NotFound
from wtforms.validators import ValidationError

from app.chat.routes import _message_json
from app.chat.services import ChatValidationError, get_conversation, mark_read, send_message
from app.extensions import socketio


def _csrf(payload) -> bool:
    try:
        validate_csrf((payload or {}).get("csrf_token"))
        return True
    except ValidationError:
        return False


def _can_chat() -> bool:
    return bool(
        current_user.is_authenticated
        and current_user.phone_verified_at
        and current_user.role in ("BUYER", "FARMER")
    )


@socketio.on("connect")
def socket_connect(auth=None):
    if not _can_chat():
        return False
    join_room(f"user:{current_user.id}")
    return True


@socketio.on("conversation:join")
def conversation_join(data):
    if not _can_chat():
        return {"error": "Authentication required."}
    try:
        conv = get_conversation(current_user, int((data or {}).get("conversation_id")))
    except NotFound, TypeError, ValueError:
        return {"error": "Conversation not found."}
    join_room(f"conversation:{conv.id}")
    return {"joined": True, "conversation_id": conv.id}


@socketio.on("message:send")
def socket_message_send(data):
    if not _can_chat():
        socketio.emit("error", {"error": "Authentication required."}, to=request.sid)
        return
    if not _csrf(data):
        socketio.emit("error", {"error": "Invalid CSRF token."}, to=request.sid)
        return
    try:
        message = send_message(
            current_user, int((data or {}).get("conversation_id")), (data or {}).get("body")
        )
        conv = get_conversation(current_user, message.conversation_id)
        socketio.emit("message:new", _message_json(message), to=f"conversation:{conv.id}")
        socketio.emit(
            "conversation:updated", {"conversation_id": conv.id}, to=f"user:{conv.buyer_id}"
        )
        socketio.emit(
            "conversation:updated",
            {"conversation_id": conv.id},
            to=f"user:{conv.listing.seller_id}",
        )
    except (NotFound, TypeError, ValueError) as exc:
        socketio.emit(
            "error",
            {
                "error": str(exc)
                if isinstance(exc, ChatValidationError)
                else "Conversation not found."
            },
            to=request.sid,
        )
    except Exception as exc:
        from flask_limiter.errors import RateLimitExceeded

        if isinstance(exc, RateLimitExceeded):
            socketio.emit("error", {"error": "Message rate limit reached."}, to=request.sid)
            return
        current_app.logger.exception("Socket message persistence failed")
        socketio.emit("error", {"error": "Message could not be sent."}, to=request.sid)


@socketio.on("conversation:read")
def socket_conversation_read(data):
    if not _can_chat():
        socketio.emit("error", {"error": "Authentication required."}, to=request.sid)
        return
    if not _csrf(data):
        socketio.emit("error", {"error": "Invalid CSRF token."}, to=request.sid)
        return
    try:
        conversation_id = int((data or {}).get("conversation_id"))
        changed = mark_read(current_user, conversation_id)
        socketio.emit(
            "conversation:read",
            {"conversation_id": conversation_id, "read": changed},
            to=request.sid,
        )
    except NotFound, TypeError, ValueError:
        socketio.emit("error", {"error": "Conversation not found."}, to=request.sid)
