"""Order lifecycle, inventory, delivery privacy, and history tests."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Conflict, NotFound

from app.chat.services import create_offer, open_conversation, transition_offer
from app.extensions import db
from app.marketplace.services import create_listing, seed_categories
from app.models import (
    Category,
    Listing,
    Order,
    OrderItem,
    OrderStatusHistory,
    Profile,
    User,
)
from app.orders.services import OrderService, OrderValidationError


def make_user(phone: str, role: str) -> User:
    user = User(phone=phone, role=role, profile=Profile())
    user.set_password("a-long-safe-passphrase")
    user.phone_verified_at = datetime.now(UTC)
    db.session.add(user)
    db.session.commit()
    return user


def make_order(buyer: User, seller: User, *, quantity="5") -> tuple[Listing, Order]:
    category = db.session.scalar(select(Category).where(Category.slug == "crops"))
    listing = create_listing(
        seller,
        {
            "category_id": category.id,
            "title": "Fresh maize harvest",
            "description": "Harvest from the farm.",
            "quantity": "10",
            "unit": "BAG",
            "grade": "A",
            "price": "150",
            "county": "Nakuru",
        },
    )
    conversation = open_conversation(buyer, listing.id)
    offer = create_offer(buyer, conversation.id, quantity, 12_500)
    _accepted, order = transition_offer(seller, conversation.id, offer.id, "accept")
    return listing, order


@pytest.fixture
def parties(app):
    seed_categories()
    buyer = make_user("+254711111111", "BUYER")
    seller = make_user("+254722222222", "FARMER")
    other_buyer = make_user("+254733333333", "BUYER")
    other_seller = make_user("+254744444444", "FARMER")
    listing, order = make_order(buyer, seller)
    return buyer, seller, other_buyer, other_seller, listing, order


def choose_delivery(buyer, order, method="PICKUP"):
    data = {"method": method}
    if method == "DELIVERY":
        data.update(
            destination_county="Nairobi",
            location_name="Westlands",
            address_text="Building and gate instructions",
            recipient_name="Buyer Recipient",
            recipient_phone="+254711111111",
        )
    return OrderService.save_delivery(buyer, order.id, data)


def simulate_verified_payment_for_test(order):
    """Set up a paid order state as if a future trusted payment boundary did so."""
    previous = order.status
    order.status = "PAID"
    order.history.append(OrderStatusHistory(previous_status=previous, status="PAID", actor_id=None))
    db.session.commit()


def test_order_from_batch4_offer_keeps_snapshot_and_reserves_once(parties):
    _buyer, _seller, _ob, _os, listing, order = parties
    db.session.refresh(listing)
    assert order.status == "CONFIRMED"
    assert order.item.title_snapshot == "Fresh maize harvest"
    assert (order.item.quantity, order.item.unit, order.item.unit_price_minor) == (5, "BAG", 12_500)
    assert order.total_minor == order.item.total_minor == 62_500
    assert listing.quantity == 5
    assert [row.status for row in order.history] == ["CONFIRMED"]


def test_order_scoping_hides_unrelated_buyers_and_sellers(parties):
    buyer, seller, other_buyer, other_seller, _listing, order = parties
    assert OrderService.get_order(buyer, order.id).id == order.id
    assert OrderService.get_order(seller, order.id).id == order.id
    with pytest.raises(NotFound):
        OrderService.get_order(other_buyer, order.id)
    with pytest.raises(NotFound):
        OrderService.get_order(other_seller, order.id)
    with pytest.raises(NotFound):
        OrderService.transition(other_buyer, order.id, "cancel")


def test_delivery_requires_complete_destination_and_only_buyer_can_set(parties):
    buyer, seller, _ob, _os, _listing, order = parties
    with pytest.raises(NotFound):
        OrderService.save_delivery(seller, order.id, {"method": "PICKUP"})
    with pytest.raises(OrderValidationError):
        OrderService.save_delivery(buyer, order.id, {"method": "DELIVERY"})
    with pytest.raises(OrderValidationError):
        OrderService.save_delivery(
            buyer,
            order.id,
            {"method": "DELIVERY", "destination_county": "Atlantis"},
        )
    delivery = choose_delivery(buyer, order, "DELIVERY")
    assert delivery.destination_county == "Nairobi"
    assert delivery.recipient_phone == buyer.phone


def test_cancellation_before_handoff_releases_reserved_stock_once(parties):
    buyer, seller, _ob, _os, listing, order = parties
    assert listing.quantity == 5
    # Consume the remaining available stock so cancellation reopens a sold listing.
    listing.quantity = 0
    listing.status = "SOLD"
    db.session.commit()
    result = OrderService.transition(buyer, order.id, "cancel")
    db.session.refresh(listing)
    assert result.status == "CANCELLED"
    assert listing.quantity == 5 and listing.status == "AVAILABLE"
    assert result.cancelled_at is not None
    assert result.history[-1].previous_status == "CONFIRMED"
    with pytest.raises(Conflict):
        OrderService.transition(seller, order.id, "cancel")
    db.session.refresh(listing)
    assert listing.quantity == 5


def test_cancel_after_physical_handoff_is_rejected_without_restock(parties):
    buyer, seller, _ob, _os, listing, order = parties
    choose_delivery(buyer, order, "DELIVERY")
    simulate_verified_payment_for_test(order)
    OrderService.transition(seller, order.id, "start_processing")
    OrderService.transition(seller, order.id, "prepare_fulfillment")
    db.session.refresh(order)
    assert order.status == "IN_TRANSIT" and order.left_seller_at is not None
    with pytest.raises(Conflict):
        OrderService.transition(buyer, order.id, "cancel")
    db.session.refresh(listing)
    assert listing.quantity == 5


def test_pickup_and_delivery_follow_distinct_valid_state_paths(parties):
    buyer, seller, _ob, _os, _listing, pickup = parties
    choose_delivery(buyer, pickup, "PICKUP")
    with pytest.raises(Conflict):
        OrderService.transition(seller, pickup.id, "start_processing")
    simulate_verified_payment_for_test(pickup)
    OrderService.transition(seller, pickup.id, "start_processing")
    OrderService.transition(seller, pickup.id, "prepare_fulfillment")
    db.session.refresh(pickup)
    assert pickup.status == "READY_FOR_PICKUP" and pickup.left_seller_at is None
    with pytest.raises(Conflict):
        OrderService.transition(buyer, pickup.id, "mark_delivered")
    OrderService.transition(seller, pickup.id, "mark_delivered")
    db.session.refresh(pickup)
    assert pickup.status == "DELIVERED" and pickup.left_seller_at is not None
    OrderService.transition(buyer, pickup.id, "confirm_completion")
    db.session.refresh(pickup)
    assert pickup.status == "COMPLETED" and pickup.completed_at is not None
    assert [row.status for row in pickup.history] == [
        "CONFIRMED",
        "PAID",
        "PROCESSING",
        "READY_FOR_PICKUP",
        "DELIVERED",
        "COMPLETED",
    ]
    with pytest.raises(Conflict):
        OrderService.transition(buyer, pickup.id, "confirm_completion")


def test_delivery_path_marks_goods_out_of_seller_stock(parties):
    buyer, seller, _ob, _os, _listing, order = parties
    delivery = choose_delivery(buyer, order, "DELIVERY")
    simulate_verified_payment_for_test(order)
    OrderService.transition(seller, order.id, "start_processing")
    with pytest.raises(Conflict):
        OrderService.transition(seller, order.id, "mark_delivered")
    OrderService.transition(seller, order.id, "prepare_fulfillment")
    db.session.refresh(order)
    db.session.refresh(delivery)
    assert order.status == "IN_TRANSIT" and order.left_seller_at is not None
    assert delivery.status == "IN_TRANSIT"
    OrderService.transition(seller, order.id, "mark_delivered")
    OrderService.transition(buyer, order.id, "confirm_completion")
    db.session.refresh(order)
    assert order.status == "COMPLETED"


def test_payment_cannot_be_marked_by_buyer_or_seller(parties):
    buyer, seller, _ob, _os, _listing, order = parties
    choose_delivery(buyer, order)
    for actor in (buyer, seller):
        with pytest.raises(OrderValidationError):
            OrderService.transition(actor, order.id, "mark_paid")
        with pytest.raises(OrderValidationError):
            OrderService.transition(actor, order.id, "PAID")
    db.session.refresh(order)
    assert order.status == "CONFIRMED"
    with pytest.raises(Conflict):
        OrderService.transition(seller, order.id, "start_processing")


def test_delivery_method_locks_after_fulfillment_starts(parties):
    buyer, seller, _ob, _os, _listing, order = parties
    choose_delivery(buyer, order, "PICKUP")
    simulate_verified_payment_for_test(order)
    OrderService.transition(seller, order.id, "start_processing")
    with pytest.raises(Conflict):
        choose_delivery(buyer, order, "DELIVERY")


def test_auto_completion_occurs_only_after_72_hour_window(parties):
    _buyer, _seller, _ob, _os, _listing, order = parties
    order.status = "DELIVERED"
    order.delivered_at = datetime.now(UTC) - timedelta(hours=73)
    db.session.add(
        OrderStatusHistory(
            order_id=order.id,
            previous_status="IN_TRANSIT",
            status="DELIVERED",
            actor_id=None,
        )
    )
    db.session.commit()
    assert OrderService.complete_delivered_orders() == 1
    db.session.refresh(order)
    assert order.status == "COMPLETED"
    entry = order.history[-1]
    assert entry.status == "COMPLETED" and entry.actor_id is None
    assert OrderService.complete_delivered_orders() == 0


def test_history_is_not_exposed_for_mutation_and_unknown_state_fails_db(parties):
    _buyer, _seller, _ob, _os, _listing, order = parties
    with pytest.raises(OrderValidationError):
        OrderService.transition(_buyer, order.id, "anything")
    history = order.history[0]
    history.status = "CANCELLED"
    with pytest.raises(ValueError, match="append-only"):
        db.session.commit()
    db.session.rollback()
    item = db.session.get(OrderItem, order.item.id)
    item.title_snapshot = "Tampered title"
    with pytest.raises(ValueError, match="immutable"):
        db.session.commit()
    db.session.rollback()
    row = OrderStatusHistory(order_id=order.id, status="FORGED", actor_id=None)
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_order_http_pages_scope_private_details_and_require_csrf(app, parties):
    buyer, seller, other_buyer, _other_seller, _listing, order = parties
    choose_delivery(buyer, order, "DELIVERY")

    def signed_in_client(user, client=None):
        client = client or app.test_client()
        if client is not None and user is not buyer:
            current_page = client.get("/account/profile")
            csrf = (
                re.search(rb'<meta name="csrf-token" content="([^"]+)"', current_page.data)
                .group(1)
                .decode()
            )
            client.post("/auth/logout", data={"csrf_token": csrf})
        login_page = client.get("/auth/login")
        token = (
            re.search(rb'<meta name="csrf-token" content="([^"]+)"', login_page.data)
            .group(1)
            .decode()
        )
        response = client.post(
            "/auth/login",
            data={
                "csrf_token": token,
                "phone": user.phone,
                "password": "a-long-safe-passphrase",
            },
        )
        assert response.status_code == 302
        return client

    buyer_client = signed_in_client(buyer)
    detail = buyer_client.get(f"/orders/{order.id}")
    assert detail.status_code == 200
    assert b"Building and gate instructions" in detail.data
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', detail.data).group(1).decode()
    assert buyer_client.post(f"/orders/{order.id}/actions/cancel").status_code == 400
    assert (
        buyer_client.post(
            f"/orders/{order.id}/actions/cancel", data={"csrf_token": token}
        ).status_code
        == 302
    )
    db.session.refresh(order)
    assert order.status == "CANCELLED"

    assert signed_in_client(other_buyer, buyer_client).get(f"/orders/{order.id}").status_code == 404
    assert signed_in_client(seller, buyer_client).get(f"/orders/{order.id}").status_code == 200


def test_unknown_action_route_cannot_set_arbitrary_status(app, parties):
    buyer, _seller, _ob, _os, _listing, order = parties
    client = app.test_client()
    login_page = client.get("/auth/login")
    login_token = (
        re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', login_page.data).group(1).decode()
    )
    client.post(
        "/auth/login",
        data={
            "csrf_token": login_token,
            "phone": buyer.phone,
            "password": "a-long-safe-passphrase",
        },
    )
    page = client.get(f"/orders/{order.id}")
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
    response = client.post(f"/orders/{order.id}/actions/PAID", data={"csrf_token": token})
    assert response.status_code == 302
    db.session.refresh(order)
    assert order.status == "CONFIRMED"
