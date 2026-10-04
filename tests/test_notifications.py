"""Persisted notification behavior and business-event integration tests."""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import NotFound

from app.chat.services import create_offer, open_conversation, send_message, transition_offer
from app.disputes.services import DisputeService
from app.extensions import db, socketio
from app.models import Notification, NotificationType
from app.notifications.services import NotificationService
from app.orders.services import OrderService
from app.payments.services import PaymentService
from app.reviews.services import ReviewService
from tests.test_chat import login_client
from tests.test_feedback import complete_order, paid_order
from tests.test_orders import choose_delivery, make_order, make_user


@pytest.fixture
def notification_parties(app):
    from app.marketplace.services import seed_categories

    seed_categories()
    buyer = make_user("+254711111111", "BUYER")
    seller = make_user("+254722222222", "FARMER")
    other = make_user("+254733333333", "BUYER")
    admin = make_user("+254744444444", "ADMIN")
    listing, order = make_order(buyer, seller)
    NotificationService.mark_all_read(buyer)
    NotificationService.mark_all_read(seller)
    return buyer, seller, other, admin, listing, order


def notifications_for(user):
    return list(
        db.session.scalars(
            select(Notification).where(Notification.user_id == user.id).order_by(Notification.id)
        )
    )


def test_notification_service_scopes_lists_counts_and_read_state(notification_parties):
    buyer, seller, _other, _admin, _listing, _order = notification_parties
    first = NotificationService.create_notification(
        buyer.id, NotificationType.MESSAGE_RECEIVED, "A message", "You have a message."
    )
    NotificationService.create_notification(
        buyer.id, NotificationType.OFFER_RECEIVED, "An offer", "An offer arrived."
    )
    NotificationService.create_notification(
        seller.id, NotificationType.REVIEW_RECEIVED, "A review", "You got a review."
    )
    db.session.commit()
    assert NotificationService.unread_count(buyer) == 2
    assert len(NotificationService.list_for_user(buyer).items) == 4
    assert NotificationService.mark_read(buyer, first.id).is_read
    assert NotificationService.mark_read(buyer, first.id).read_at is not None
    assert NotificationService.unread_count(buyer) == 1
    with pytest.raises(NotFound):
        NotificationService.get_for_user(buyer, notifications_for(seller)[0].id)
    with pytest.raises(NotFound):
        NotificationService.mark_read(buyer, notifications_for(seller)[0].id)
    assert NotificationService.mark_all_read(buyer) == 1
    assert NotificationService.unread_count(buyer) == 0


def test_notification_constraints_reject_unknown_types_and_bad_targets(notification_parties):
    buyer, *_ = notification_parties
    with pytest.raises(ValueError):
        NotificationService.create_notification(buyer.id, "FUTURE_TYPE", "Title", "Body")
    with pytest.raises(ValueError):
        NotificationService.create_notification(
            buyer.id,
            NotificationType.MESSAGE_RECEIVED,
            "Title",
            "Body",
            target_type="https://example.test",
            target_id=1,
        )
    row = Notification(
        user_id=buyer.id,
        notification_type="INVALID",
        title="Title",
        body="Body",
    )
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_message_and_offer_events_notify_only_recipients(notification_parties):
    buyer, seller, _other, _admin, listing, _order = notification_parties
    conversation = open_conversation(buyer, listing.id)
    buyer_before = len(notifications_for(buyer))
    seller_before = len(notifications_for(seller))
    send_message(buyer, conversation.id, "Are these fresh?")
    assert (
        notifications_for(seller)[-1].notification_type == NotificationType.MESSAGE_RECEIVED.value
    )
    assert len(notifications_for(seller)) == seller_before + 1
    assert len(notifications_for(buyer)) == buyer_before

    offer = create_offer(buyer, conversation.id, 1, 1000)
    assert notifications_for(seller)[-1].notification_type == NotificationType.OFFER_RECEIVED.value
    transition_offer(seller, conversation.id, offer.id, "reject")
    assert notifications_for(buyer)[-1].notification_type == NotificationType.OFFER_REJECTED.value

    counter_parent = create_offer(buyer, conversation.id, 1, 1000)
    _old, counter = transition_offer(
        seller,
        conversation.id,
        counter_parent.id,
        "counter",
        counter_quantity=1,
        counter_unit_price_minor=1100,
    )
    assert counter is not None
    assert notifications_for(buyer)[-1].notification_type == NotificationType.OFFER_COUNTERED.value
    transition_offer(buyer, conversation.id, counter.id, "accept")
    assert notifications_for(seller)[-1].notification_type == NotificationType.OFFER_ACCEPTED.value
    assert any(
        n.notification_type == NotificationType.ORDER_CONFIRMED.value
        for n in notifications_for(buyer)
    )


def test_order_payment_and_refund_notifications_follow_trusted_transitions(notification_parties):
    buyer, seller, _other, admin, _listing, order = notification_parties
    assert any(
        n.notification_type == NotificationType.ORDER_CONFIRMED.value
        for n in notifications_for(buyer)
    )
    choose_delivery(buyer, order)
    payment, _ = PaymentService.initiate(buyer, order.id)
    assert not any(
        n.notification_type == NotificationType.PAYMENT_SUCCEEDED.value
        for n in notifications_for(buyer)
    )
    PaymentService.simulate_mock_event(payment.id, "success")
    assert any(
        n.notification_type == NotificationType.PAYMENT_SUCCEEDED.value
        for n in notifications_for(buyer)
    )
    assert any(
        n.notification_type == NotificationType.PAYMENT_SUCCEEDED.value
        for n in notifications_for(seller)
    )
    refund = PaymentService.refund(admin, payment.id)
    assert refund.refund_pending
    assert not any(
        n.notification_type == NotificationType.REFUND_SUCCEEDED.value
        for n in notifications_for(buyer)
    )
    PaymentService.simulate_mock_event(payment.id, "refunded")
    assert notifications_for(buyer)[-1].notification_type == NotificationType.REFUND_SUCCEEDED.value

    _next_listing, next_order = make_order(buyer, seller)
    paid_order(buyer, next_order)
    OrderService.transition(seller, next_order.id, "start_processing")
    assert notifications_for(buyer)[-1].notification_type == NotificationType.ORDER_PROCESSING.value
    OrderService.transition(seller, next_order.id, "prepare_fulfillment")
    assert (
        notifications_for(buyer)[-1].notification_type
        == NotificationType.ORDER_READY_FOR_PICKUP.value
    )
    OrderService.transition(seller, next_order.id, "mark_delivered")
    assert notifications_for(buyer)[-1].notification_type == NotificationType.ORDER_DELIVERED.value
    OrderService.transition(buyer, next_order.id, "confirm_completion")
    assert notifications_for(seller)[-1].notification_type == NotificationType.ORDER_COMPLETED.value


def test_failed_payment_review_and_dispute_events(notification_parties):
    buyer, seller, _other, admin, listing, order = notification_parties
    choose_delivery(buyer, order)
    payment, _ = PaymentService.initiate(buyer, order.id)
    PaymentService.simulate_mock_event(payment.id, "failed")
    assert notifications_for(buyer)[-1].notification_type == NotificationType.PAYMENT_FAILED.value
    payment, _ = PaymentService.initiate(buyer, order.id)
    PaymentService.simulate_mock_event(payment.id, "success")
    dispute = DisputeService.create_dispute(buyer, order.id, "OTHER", "Need help")
    assert notifications_for(seller)[-1].notification_type == NotificationType.DISPUTE_OPENED.value
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    assert (
        notifications_for(buyer)[-1].notification_type
        == NotificationType.DISPUTE_UNDER_REVIEW.value
    )
    DisputeService.admin_transition(admin, dispute.id, "resolve_for_seller", "Reviewed")
    assert (
        notifications_for(seller)[-1].notification_type
        == NotificationType.DISPUTE_RESOLVED_FOR_SELLER.value
    )

    second_listing, second_order = make_order(buyer, seller)
    paid_order(buyer, second_order)
    complete_order(buyer, seller, second_order)
    ReviewService.create_review(buyer, second_order.id, 5, "Good quality")
    assert notifications_for(seller)[-1].notification_type == NotificationType.REVIEW_RECEIVED.value
    assert listing.id != second_listing.id


def test_buyer_dispute_resolution_notification_waits_for_refund_confirmation(
    notification_parties,
):
    buyer, seller, _other, admin, _listing, _order = notification_parties
    _listing, order = make_order(buyer, seller)
    payment = paid_order(buyer, order)
    dispute = DisputeService.create_dispute(buyer, order.id, "OTHER", "Please review")
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    DisputeService.admin_transition(admin, dispute.id, "resolve_for_buyer", "Refund approved")
    assert not any(
        item.notification_type == NotificationType.DISPUTE_RESOLVED_FOR_BUYER.value
        for item in notifications_for(buyer)
    )
    assert not any(
        item.notification_type == NotificationType.REFUND_SUCCEEDED.value
        for item in notifications_for(buyer)
    )
    assert payment.refund_pending
    PaymentService.simulate_mock_event(payment.id, "refunded")
    assert any(
        item.notification_type == NotificationType.REFUND_SUCCEEDED.value
        for item in notifications_for(buyer)
    )
    assert any(
        item.notification_type == NotificationType.DISPUTE_RESOLVED_FOR_BUYER.value
        for item in notifications_for(seller)
    )


def test_notification_http_access_csrf_and_safe_target(notification_parties, client):
    buyer, _seller, other, _admin, _listing, order = notification_parties
    notification = NotificationService.create_notification(
        buyer.id,
        NotificationType.ORDER_CONFIRMED,
        "Order confirmed",
        "Order is ready.",
        target_type="order",
        target_id=order.id,
    )
    db.session.commit()
    assert client.get("/notifications").status_code == 302
    login_client(client, buyer)
    response = client.get("/notifications")
    assert response.status_code == 200
    assert b"Order confirmed" in response.data
    assert b'data-notification-count="1"' in client.get("/").data
    assert (
        client.get(f"/notifications/{notification.id}/open")
        .headers["Location"]
        .endswith(f"/orders/{order.id}")
    )
    assert client.post(f"/notifications/{notification.id}/read").status_code == 400
    token = re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', response.data).group(1).decode()
    read = client.post(f"/notifications/{notification.id}/read", data={"csrf_token": token})
    assert read.status_code == 302
    assert NotificationService.unread_count(buyer) == 0
    NotificationService.create_notification(
        buyer.id,
        NotificationType.PAYMENT_FAILED,
        "Payment update",
        "Please retry your payment.",
    )
    db.session.commit()
    assert client.post("/notifications/read-all").status_code == 400
    assert client.post("/notifications/read-all", data={"csrf_token": token}).status_code == 302
    assert NotificationService.unread_count(buyer) == 0
    client.post("/auth/logout", data={"csrf_token": token})
    login_client(client, other)
    other_page = client.get("/notifications")
    assert other_page.status_code == 200
    assert b"Order confirmed" not in other_page.data
    other_token = (
        re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', other_page.data).group(1).decode()
    )
    assert (
        client.post(
            f"/notifications/{notification.id}/read", data={"csrf_token": other_token}
        ).status_code
        == 404
    )


def test_notification_pagination_is_bounded_and_text_is_escaped(notification_parties, client):
    buyer, *_ = notification_parties
    existing_count = len(NotificationService.list_for_user(buyer).items)
    for number in range(22):
        NotificationService.create_notification(
            buyer.id,
            NotificationType.MESSAGE_RECEIVED,
            f"Update {number}",
            "<script>alert('no')</script>" if number == 0 else f"Update body {number}",
        )
    db.session.commit()
    first = NotificationService.list_for_user(buyer, page=1, per_page=500)
    second = NotificationService.list_for_user(buyer, page=2, per_page=500)
    assert first.per_page == 50 and len(first.items) == existing_count + 22
    assert len(second.items) == 0
    login_client(client, buyer)
    rendered = client.get("/notifications?page=2")
    assert b"&lt;script&gt;" in rendered.data
    assert b"<script>alert('no')</script>" not in rendered.data


def test_notification_socket_delivery_is_private_and_after_commit(notification_parties):
    buyer, _seller, _other, _admin, _listing, _order = notification_parties
    app = __import__("flask").current_app._get_current_object()
    buyer_client = app.test_client()
    anonymous_socket = socketio.test_client(app, flask_test_client=app.test_client())
    assert not anonymous_socket.is_connected()
    login_client(buyer_client, buyer)
    buyer_socket = socketio.test_client(app, flask_test_client=buyer_client)
    assert buyer_socket.is_connected()
    row = NotificationService.create_notification(
        buyer.id, NotificationType.MESSAGE_RECEIVED, "Private", "Only for the buyer."
    )
    assert not buyer_socket.get_received()
    db.session.commit()
    received = buyer_socket.get_received()
    assert any(
        event["name"] == "notification:new"
        and event["args"][0]["title"] == row.title
        for event in received
    )
    buyer_socket.disconnect()


def test_socket_delivery_failure_does_not_undo_persisted_notification(
    notification_parties, monkeypatch
):
    buyer, *_ = notification_parties

    def fail_emit(*_args, **_kwargs):
        raise RuntimeError("socket unavailable")

    monkeypatch.setattr(socketio, "emit", fail_emit)
    row = NotificationService.create_notification(
        buyer.id, NotificationType.MESSAGE_RECEIVED, "Saved", "Still available in center."
    )
    db.session.commit()
    assert db.session.get(Notification, row.id) is not None
