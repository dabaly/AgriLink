"""HTTP routes for conversations, messages, offers, and confirmed orders."""

from __future__ import annotations

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import Conflict, NotFound

from app.chat import chat_bp
from app.chat.forms import MessageForm, OfferActionForm, OfferForm
from app.chat.services import (
    ChatValidationError,
    create_offer,
    get_conversation,
    list_conversations,
    list_offers,
    mark_read,
    message_history,
    open_conversation,
    send_message,
    transition_offer,
    unread_count,
)
from app.extensions import socketio
from app.marketplace.services import parse_kes_to_minor
from app.models.trading import Conversation, Message, Offer, Order


def _message_json(message: Message) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }


def _offer_json(offer: Offer) -> dict:
    return {
        "id": offer.id,
        "conversation_id": offer.conversation_id,
        "proposer_id": offer.proposer_id,
        "parent_offer_id": offer.parent_offer_id,
        "quantity": offer.quantity,
        "unit": offer.unit,
        "unit_price_minor": offer.unit_price_minor,
        "total_minor": offer.total_minor,
        "status": offer.status,
        "expires_at": offer.expires_at.isoformat(),
        "created_at": offer.created_at.isoformat(),
    }


def _notify_conversation(conv: Conversation, event: str, payload: dict) -> None:
    socketio.emit(event, payload, to=f"conversation:{conv.id}")
    socketio.emit("conversation:updated", {"conversation_id": conv.id}, to=f"user:{conv.buyer_id}")
    socketio.emit(
        "conversation:updated", {"conversation_id": conv.id}, to=f"user:{conv.listing.seller_id}"
    )


@chat_bp.post("/listings/<int:listing_id>/conversation")
@login_required
def start_conversation(listing_id: int):
    try:
        conv = open_conversation(current_user, listing_id)
    except NotFound:
        abort(404)
    except ChatValidationError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("marketplace.detail", listing_id=listing_id))
    return redirect(url_for("chat.conversation_detail", conversation_id=conv.id))


@chat_bp.get("/conversations")
@login_required
def conversations():
    if current_user.phone_verified_at is None or current_user.role not in ("BUYER", "FARMER"):
        abort(404)
    page = _page_arg(request.args.get("page"))
    pagination = list_conversations(current_user, page=page)
    return render_template(
        "chat/conversations.html", pagination=pagination, unread_total=unread_count(current_user)
    )


@chat_bp.get("/conversations/<int:conversation_id>")
@login_required
def conversation_detail(conversation_id: int):
    try:
        conv = get_conversation(current_user, conversation_id)
        page = _page_arg(request.args.get("page"))
        messages = message_history(current_user, conversation_id, page=page)
        offers = list_offers(current_user, conversation_id)
    except NotFound:
        abort(404)
    form = MessageForm()
    offer_form = OfferForm()
    return render_template(
        "chat/detail.html",
        conversation=conv,
        messages=messages,
        offers=offers,
        form=form,
        offer_form=offer_form,
        unread_total=unread_count(current_user),
    )


@chat_bp.post("/conversations/<int:conversation_id>/messages")
@login_required
def post_message(conversation_id: int):
    form = MessageForm()
    try:
        if not form.validate_on_submit():
            raise ChatValidationError("Enter a message of at most 2,000 characters.")
        message = send_message(current_user, conversation_id, form.body.data)
        conv = get_conversation(current_user, conversation_id)
        _notify_conversation(conv, "message:new", _message_json(message))
        flash("Message sent.", "success")
    except NotFound:
        abort(404)
    except Conflict as exc:
        abort(429 if exc.code == 429 else 409, description=str(exc))
    except ChatValidationError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("chat.conversation_detail", conversation_id=conversation_id))


@chat_bp.post("/api/conversations/<int:conversation_id>/messages")
@login_required
def post_message_api(conversation_id: int):
    data = request.get_json(silent=True) or {}
    try:
        message = send_message(current_user, conversation_id, data.get("body"))
        conv = get_conversation(current_user, conversation_id)
        payload = _message_json(message)
        _notify_conversation(conv, "message:new", payload)
        return jsonify(payload), 201
    except NotFound:
        abort(404)
    except ChatValidationError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        from flask_limiter.errors import RateLimitExceeded

        if isinstance(exc, RateLimitExceeded):
            return jsonify({"error": "Message rate limit reached."}), 429
        raise


@chat_bp.post("/api/conversations/<int:conversation_id>/read")
@login_required
def mark_conversation_read(conversation_id: int):
    try:
        changed = mark_read(current_user, conversation_id)
    except NotFound:
        abort(404)
    return jsonify({"read": changed})


@chat_bp.post("/conversations/<int:conversation_id>/offers")
@login_required
def post_offer(conversation_id: int):
    form = OfferForm()
    try:
        if not form.validate_on_submit():
            raise ChatValidationError("Enter a valid offer quantity and KES unit price.")
        price_minor = parse_kes_to_minor(form.unit_price.data)
        offer = create_offer(current_user, conversation_id, form.quantity.data, price_minor)
        conv = get_conversation(current_user, conversation_id)
        _notify_conversation(conv, "offer:updated", _offer_json(offer))
        flash("Offer sent. It expires in 48 hours.", "success")
    except NotFound:
        abort(404)
    except (ChatValidationError, ValueError) as exc:
        flash(str(exc), "warning")
    except Conflict as exc:
        abort(409, description=str(exc))
    return redirect(url_for("chat.conversation_detail", conversation_id=conversation_id))


@chat_bp.get("/api/conversations/<int:conversation_id>/offers")
@login_required
def offers_api(conversation_id: int):
    try:
        offers = list_offers(current_user, conversation_id)
    except NotFound:
        abort(404)
    return jsonify({"offers": [_offer_json(offer) for offer in offers]})


@chat_bp.post("/conversations/<int:conversation_id>/offers/<int:offer_id>/<action>")
@login_required
def offer_action(conversation_id: int, offer_id: int, action: str):
    form = OfferForm()
    try:
        if action == "counter":
            if not form.validate_on_submit():
                raise ChatValidationError("Enter a valid counter offer.")
            price_minor = parse_kes_to_minor(form.unit_price.data)
            offer, created_order = transition_offer(
                current_user,
                conversation_id,
                offer_id,
                action,
                counter_quantity=form.quantity.data,
                counter_unit_price_minor=price_minor,
            )
        else:
            if not OfferActionForm().validate_on_submit():
                abort(400)
            offer, created_order = transition_offer(current_user, conversation_id, offer_id, action)
        conv = get_conversation(current_user, conversation_id)
        _notify_conversation(conv, "offer:updated", _offer_json(offer))
        if created_order and isinstance(created_order, Offer):
            _notify_conversation(conv, "offer:updated", _offer_json(created_order))
        if created_order and isinstance(created_order, Order):
            _notify_conversation(
                conv,
                "order:created",
                {"order_id": created_order.id, "status": created_order.status},
            )
            flash("Offer accepted. The order is confirmed.", "success")
        else:
            flash(f"Offer {action}ed.", "success")
    except NotFound:
        abort(404)
    except (ChatValidationError, ValueError) as exc:
        flash(str(exc), "warning")
    except Conflict as exc:
        abort(409, description=str(exc))
    return redirect(url_for("chat.conversation_detail", conversation_id=conversation_id))


def _page_arg(value) -> int:
    try:
        return max(1, min(int(value or 1), 100_000))
    except TypeError, ValueError:
        return 1
