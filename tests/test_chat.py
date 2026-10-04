"""Conversation, Socket.IO, offer, and stock transaction coverage."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from werkzeug.exceptions import Conflict, NotFound

from app.chat.services import (
    ChatValidationError,
    create_offer,
    expire_pending_offers,
    get_conversation,
    list_offers,
    mark_read,
    message_history,
    open_conversation,
    send_message,
    transition_offer,
    unread_count,
)
from app.extensions import db, socketio
from app.marketplace.services import create_listing, seed_categories
from app.models import (
    Category,
    Message,
    Offer,
    Order,
    Profile,
    User,
)


def make_user(phone, role, *, verified=True):
    user = User(phone=phone, role=role, profile=Profile())
    user.set_password("a-long-safe-passphrase")
    if verified:
        user.phone_verified_at = datetime.now(UTC)
    db.session.add(user)
    db.session.commit()
    return user


def make_listing(seller, **overrides):
    category = db.session.scalar(select(Category).where(Category.slug == "crops"))
    data = {
        "category_id": category.id,
        "title": "Fresh maize harvest",
        "description": "Harvest from the farm.",
        "quantity": "20",
        "unit": "BAG",
        "grade": "A",
        "price": "150",
        "county": "Nakuru",
    }
    data.update(overrides)
    return create_listing(seller, data)


def login_client(client, user):
    response = client.get("/auth/login")
    token = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data).group(1).decode()
    response = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "phone": user.phone,
            "password": "a-long-safe-passphrase",
        },
    )
    assert response.status_code == 302


@pytest.fixture
def actors(app):
    seed_categories()
    farmer = make_user("+254711111111", "FARMER")
    buyer = make_user("+254722222222", "BUYER")
    other_farmer = make_user("+254733333333", "FARMER")
    listing = make_listing(farmer)
    conversation = open_conversation(buyer, listing.id)
    return farmer, buyer, other_farmer, listing, conversation


def test_conversation_open_is_idempotent_and_participant_scoped(actors):
    farmer, buyer, other, listing, conv = actors
    assert open_conversation(buyer, listing.id).id == conv.id
    assert get_conversation(farmer, conv.id).id == conv.id
    with pytest.raises(NotFound):
        get_conversation(other, conv.id)
    with pytest.raises(NotFound):
        open_conversation(farmer, listing.id)


def test_messages_are_persisted_unread_then_marked_read(actors):
    farmer, buyer, _other, _listing, conv = actors
    message = send_message(buyer, conv.id, "  Is this maize ready?  ")
    assert message.body == "Is this maize ready?"
    assert unread_count(farmer) == 1
    assert unread_count(buyer) == 0
    assert mark_read(farmer, conv.id) == 1
    assert unread_count(farmer) == 0
    assert message_history(farmer, conv.id).items[0].id == message.id
    with pytest.raises(ChatValidationError):
        send_message(buyer, conv.id, "<script>bad</script>")


def test_conversation_and_message_idor_are_hidden(actors):
    _farmer, buyer, other, _listing, conv = actors
    with pytest.raises(NotFound):
        send_message(other, conv.id, "No access")
    with pytest.raises(NotFound):
        mark_read(other, conv.id)
    with pytest.raises(NotFound):
        get_conversation(buyer, conv.id + 100)


def test_offers_counter_expiry_and_reject(actors):
    farmer, buyer, _other, listing, conv = actors
    original = create_offer(buyer, conv.id, "4", 12_500)
    assert original.total_minor == 50_000
    with pytest.raises(Conflict):
        create_offer(farmer, conv.id, "1", 10_000)
    old, counter = transition_offer(
        farmer,
        conv.id,
        original.id,
        "counter",
        counter_quantity="3",
        counter_unit_price_minor=11_000,
    )
    assert old.status == "COUNTERED"
    assert counter.parent_offer_id == old.id
    rejected, order = transition_offer(buyer, conv.id, counter.id, "reject")
    assert rejected.status == "REJECTED" and order is None
    expired = create_offer(buyer, conv.id, "2", 12_000)
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.session.commit()
    assert expire_pending_offers() == 1
    assert list_offers(farmer, conv.id)[-1].status == "EXPIRED"


def test_accept_offer_atomically_creates_snapshot_order_and_reduces_stock(actors):
    farmer, buyer, _other, listing, conv = actors
    offer = create_offer(buyer, conv.id, "7", 12_345)
    accepted, order = transition_offer(farmer, conv.id, offer.id, "accept")
    db.session.refresh(listing)
    assert accepted.status == "ACCEPTED"
    assert listing.quantity == 13
    assert listing.status == "AVAILABLE"
    assert order.status == "CONFIRMED" and order.total_minor == 86_415
    assert order.item.title_snapshot == "Fresh maize harvest"
    assert (order.item.quantity, order.item.unit_price_minor, order.item.total_minor) == (
        7,
        12_345,
        86_415,
    )
    assert order.history[0].status == "CONFIRMED"
    assert order.history[0].actor_id == farmer.id
    with pytest.raises(Conflict):
        transition_offer(farmer, conv.id, offer.id, "accept")
    assert db.session.scalar(select(db.func.count(Order.id))) == 1


def test_accept_last_quantity_marks_sold_and_later_stock_conflicts(actors):
    farmer, buyer, _other, listing, conv = actors
    offer = create_offer(buyer, conv.id, "20", 15_000)
    _, order = transition_offer(farmer, conv.id, offer.id, "accept")
    db.session.refresh(listing)
    assert listing.quantity == 0 and listing.status == "SOLD"
    assert order.total_minor == 300_000
    with pytest.raises(Conflict):
        create_offer(buyer, conv.id, "1", 1_000)


def test_offer_ids_are_scoped_and_only_other_side_can_accept(actors):
    farmer, buyer, other, _listing, conv = actors
    offer = create_offer(buyer, conv.id, "1", 15_000)
    with pytest.raises(NotFound):
        transition_offer(buyer, conv.id, offer.id, "accept")
    with pytest.raises(NotFound):
        transition_offer(other, conv.id, offer.id, "accept")


def test_offer_stock_conflict_rolls_back_offer_and_order(actors):
    farmer, buyer, _other, listing, conv = actors
    offer = create_offer(buyer, conv.id, "10", 15_000)
    listing.quantity = 3
    db.session.commit()
    with pytest.raises(Conflict):
        transition_offer(farmer, conv.id, offer.id, "accept")
    db.session.refresh(listing)
    db.session.refresh(offer)
    assert listing.quantity == 3
    assert offer.status == "PENDING"
    assert db.session.scalar(select(db.func.count(Order.id))) == 0


def test_socket_requires_login_and_csrf_and_persists_before_broadcast(app, actors):
    farmer, buyer, _other, _listing, conv = actors
    anonymous = socketio.test_client(app)
    assert not anonymous.is_connected()
    client = app.test_client()
    login_client(client, buyer)
    page = client.get(f"/conversations/{conv.id}")
    assert page.status_code == 200, page.data.decode()
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
    live = socketio.test_client(app, flask_test_client=client)
    assert live.is_connected()
    live.emit("conversation:join", {"conversation_id": conv.id})
    live.emit("message:send", {"conversation_id": conv.id, "body": "No token"})
    assert any(item["name"] == "error" for item in live.get_received())
    live.emit(
        "message:send",
        {"conversation_id": conv.id, "body": "Socket persisted", "csrf_token": token},
    )
    assert db.session.scalar(select(Message).where(Message.body == "Socket persisted")) is not None
    events = live.get_received()
    assert any(item["name"] == "message:new" for item in events)


def test_http_messages_csrf_idor_and_offer_route(app, actors):
    farmer, buyer, other, listing, conv = actors
    client = app.test_client()
    login_client(client, buyer)
    page = client.get(f"/conversations/{conv.id}")
    assert page.status_code == 200, page.data.decode()
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
    response = client.post(
        f"/conversations/{conv.id}/messages", data={"csrf_token": token, "body": "HTTP fallback"}
    )
    assert response.status_code == 302
    assert db.session.scalar(select(Message).where(Message.body == "HTTP fallback"))
    assert (
        client.post(f"/conversations/{conv.id}/messages", data={"body": "no csrf"}).status_code
        == 400
    )
    assert client.get(f"/conversations/{conv.id + 100}").status_code == 404
    response = client.post(
        f"/conversations/{conv.id}/offers",
        data={"csrf_token": token, "quantity": "3", "unit_price": "123.45"},
    )
    assert response.status_code == 302
    offer = db.session.scalar(select(Offer).where(Offer.conversation_id == conv.id))
    assert offer.unit_price_minor == 12345 and offer.total_minor == 37035
