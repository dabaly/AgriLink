"""Transaction review and dispute boundary tests."""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Conflict, NotFound

from app.disputes.services import DisputeService, DisputeValidationError
from app.extensions import db
from app.models import Dispute, DisputeHistory
from app.models.payment import PaymentStatus
from app.orders.services import OrderService
from app.payments.providers import ProviderEvent
from app.payments.services import PaymentService
from app.reviews.services import ReviewService, ReviewValidationError
from tests.test_chat import login_client
from tests.test_orders import choose_delivery, make_order, make_user


@pytest.fixture
def feedback_parties(app):
    from app.marketplace.services import seed_categories

    seed_categories()
    buyer = make_user("+254711111111", "BUYER")
    seller = make_user("+254722222222", "FARMER")
    other = make_user("+254733333333", "BUYER")
    admin = make_user("+254744444444", "ADMIN")
    listing, order = make_order(buyer, seller)
    return buyer, seller, other, admin, listing, order


def paid_order(buyer, order, method="PICKUP"):
    choose_delivery(buyer, order, method)
    payment, _ = PaymentService.initiate(buyer, order.id)
    PaymentService.simulate_mock_event(payment.id, "success")
    db.session.refresh(order)
    return payment


def complete_order(buyer, seller, order):
    if order.status == "CONFIRMED":
        paid_order(buyer, order)
    OrderService.transition(seller, order.id, "start_processing")
    OrderService.transition(seller, order.id, "prepare_fulfillment")
    OrderService.transition(seller, order.id, "mark_delivered")
    OrderService.transition(buyer, order.id, "confirm_completion")
    db.session.refresh(order)


def test_review_requires_completed_owned_order_and_is_unique(feedback_parties):
    buyer, seller, other, _admin, listing, order = feedback_parties
    with pytest.raises(Conflict):
        ReviewService.create_review(buyer, order.id, 5, "Good produce")
    paid_order(buyer, order)
    complete_order(buyer, seller, order)
    review = ReviewService.create_review(buyer, order.id, 5, "  Good produce\n  ")
    assert (review.rating, review.body, review.seller_id, review.listing_id) == (
        5,
        "Good produce",
        seller.id,
        listing.id,
    )
    with pytest.raises(Conflict):
        ReviewService.create_review(buyer, order.id, 4, "Duplicate")
    with pytest.raises(NotFound):
        ReviewService.create_review(other, order.id, 5, "Not purchased")
    with pytest.raises(NotFound):
        ReviewService.create_review(seller, order.id, 5, "Self review")
    assert ReviewService.public_reviews_for_listing(listing.id)[0].id == review.id
    assert ReviewService.seller_review_summary(seller.id) == (pytest.approx(5), 1)


@pytest.mark.parametrize("rating", [0, 6, True, 4.5, "5"])
def test_review_rejects_invalid_ratings(feedback_parties, rating):
    buyer, seller, _other, _admin, _listing, order = feedback_parties
    paid_order(buyer, order)
    complete_order(buyer, seller, order)
    with pytest.raises(ReviewValidationError):
        ReviewService.create_review(buyer, order.id, rating, "Useful note")


def test_review_rejects_markup_and_review_is_immutable(feedback_parties):
    buyer, seller, _other, _admin, _listing, order = feedback_parties
    paid_order(buyer, order)
    complete_order(buyer, seller, order)
    with pytest.raises(ReviewValidationError):
        ReviewService.create_review(buyer, order.id, 5, "<script>alert(1)</script>")
    review = ReviewService.create_review(buyer, order.id, 5, "Fresh harvest")
    review.body = "changed"
    with pytest.raises(ValueError):
        db.session.commit()
    db.session.rollback()


def test_dispute_idor_eligibility_and_seller_resolution(feedback_parties):
    buyer, seller, other, admin, _listing, order = feedback_parties
    with pytest.raises(Conflict):
        DisputeService.create_dispute(buyer, order.id, "OTHER", "Issue")
    paid_order(buyer, order)
    dispute = DisputeService.create_dispute(buyer, order.id, "QUALITY_ISSUE", "Produce was damaged")
    assert order.status == "DISPUTED"
    assert DisputeService.get_for_order(seller, order.id).id == dispute.id
    with pytest.raises(NotFound):
        DisputeService.get_for_order(other, order.id)
    with pytest.raises(NotFound):
        DisputeService.get_for_participant(other, dispute.id)
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    resolved = DisputeService.admin_transition(
        admin, dispute.id, "resolve_for_seller", "Evidence supports seller"
    )
    db.session.refresh(order)
    assert resolved.status == "RESOLVED_FOR_SELLER"
    assert order.status == "COMPLETED"
    assert [row.status for row in order.history][-1] == "COMPLETED"
    assert db.session.scalar(select(DisputeHistory).where(DisputeHistory.dispute_id == dispute.id))
    with pytest.raises(Conflict):
        DisputeService.admin_transition(admin, dispute.id, "resolve_for_seller", "Again")


def test_buyer_dispute_resolution_waits_for_provider_and_releases_pre_handoff_stock(
    feedback_parties,
):
    buyer, _seller, _other, admin, listing, order = feedback_parties
    payment = paid_order(buyer, order)
    dispute = DisputeService.create_dispute(buyer, order.id, "ITEM_NOT_AS_DESCRIBED", "Wrong grade")
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    pending = DisputeService.admin_transition(
        admin, dispute.id, "resolve_for_buyer", "Refund approved"
    )
    db.session.refresh(payment)
    assert pending.status == "UNDER_REVIEW" and pending.resolution_pending
    assert payment.status == PaymentStatus.SUCCEEDED.value and payment.refund_pending
    refund_event = ProviderEvent(
        "refund-event",
        "refund.confirmed",
        payment.provider_reference,
        "REFUNDED",
        payment.amount_minor,
        "KES",
        "refund-reference",
    )
    PaymentService.process_event("MOCK", refund_event, payload=b"trusted refund confirmation")
    db.session.refresh(order)
    db.session.refresh(listing)
    db.session.refresh(payment)
    db.session.refresh(dispute)
    assert order.status == "CANCELLED"
    assert payment.status == PaymentStatus.REFUNDED.value
    assert dispute.status == "RESOLVED_FOR_BUYER" and not dispute.resolution_pending
    assert listing.quantity == 10


def test_buyer_refund_after_handoff_does_not_restock(feedback_parties):
    buyer, seller, _other, admin, listing, order = feedback_parties
    payment = paid_order(buyer, order, method="DELIVERY")
    OrderService.transition(seller, order.id, "start_processing")
    OrderService.transition(seller, order.id, "prepare_fulfillment")
    assert order.left_seller_at is not None
    dispute = DisputeService.create_dispute(buyer, order.id, "DELIVERY_ISSUE", "Not received")
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    DisputeService.admin_transition(admin, dispute.id, "resolve_for_buyer", "Refund approved")
    event = ProviderEvent(
        "refund-event-post-handoff",
        "refund.confirmed",
        payment.provider_reference,
        "REFUNDED",
        payment.amount_minor,
        "KES",
        "refund-reference-post-handoff",
    )
    PaymentService.process_event("MOCK", event, payload=b"refund after handoff")
    db.session.refresh(listing)
    assert listing.quantity == 5


def test_failed_provider_refund_leaves_dispute_under_review(feedback_parties):
    buyer, _seller, _other, admin, _listing, order = feedback_parties
    payment = paid_order(buyer, order)
    dispute = DisputeService.create_dispute(buyer, order.id, "PAYMENT_ISSUE", "Payment concern")
    DisputeService.admin_transition(admin, dispute.id, "start_review")
    DisputeService.admin_transition(admin, dispute.id, "resolve_for_buyer", "Refund approved")
    failed = ProviderEvent(
        "refund-failed-event",
        "refund.failed",
        payment.provider_reference,
        "FAILED",
        payment.amount_minor,
        "KES",
        failure_code="PROVIDER_DECLINED",
    )
    PaymentService.process_event("MOCK", failed, payload=b"refund failure")
    db.session.refresh(payment)
    db.session.refresh(dispute)
    db.session.refresh(order)
    assert payment.status == PaymentStatus.SUCCEEDED.value
    assert not payment.refund_pending
    assert dispute.status == "UNDER_REVIEW" and not dispute.resolution_pending
    assert order.status == "DISPUTED"


def test_dispute_constraints_and_service_validation(feedback_parties):
    buyer, _seller, _other, _admin, _listing, order = feedback_parties
    paid_order(buyer, order)
    with pytest.raises(DisputeValidationError):
        DisputeService.create_dispute(buyer, order.id, "BOGUS", "Issue")
    dispute = DisputeService.create_dispute(buyer, order.id, "OTHER", "Issue")
    duplicate = Dispute(
        order_id=order.id,
        opened_by=buyer.id,
        reason="OTHER",
        description="Duplicate",
    )
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Dispute, dispute.id) is not None


def test_feedback_pages_and_csrf_protection(feedback_parties, client):
    buyer, seller, _other, admin, _listing, order = feedback_parties
    paid_order(buyer, order)
    complete_order(buyer, seller, order)
    login_client(client, buyer)
    response = client.get(f"/orders/{order.id}")
    assert response.status_code == 200 and b"Leave a review" in response.data
    no_csrf = client.post(f"/orders/{order.id}/reviews", data={"rating": 5, "body": "Great"})
    assert no_csrf.status_code == 400
    client.post(
        "/auth/logout",
        headers={
            "X-CSRFToken": re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', response.data)
            .group(1)
            .decode()
        },
    )
    login_client(client, buyer)
    response = client.get(f"/orders/{order.id}")
    token = response.data.decode().split('name="csrf_token" type="hidden" value="')[1].split('"')[0]
    submitted = client.post(
        f"/orders/{order.id}/reviews",
        data={"csrf_token": token, "rating": 5, "body": "A public review"},
    )
    assert submitted.status_code in {302, 303}
    response = client.get(f"/orders/{order.id}")
    token = re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', response.data).group(1).decode()
    client.post("/auth/logout", data={"csrf_token": token})
    login_client(client, admin)
    assert client.get("/admin/disputes").status_code == 200


def test_dispute_and_admin_actions_are_csrf_protected_and_scoped(feedback_parties, client):
    buyer, seller, other, admin, _listing, order = feedback_parties
    paid_order(buyer, order)
    login_client(client, buyer)
    order_page = client.get(f"/orders/{order.id}")
    missing_token = client.post(
        f"/orders/{order.id}/disputes",
        data={"reason": "OTHER", "description": "The item has an issue"},
    )
    assert missing_token.status_code == 400
    token = re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', order_page.data).group(1).decode()
    opened = client.post(
        f"/orders/{order.id}/disputes",
        data={"csrf_token": token, "reason": "OTHER", "description": "The item has an issue"},
    )
    assert opened.status_code in {302, 303}
    dispute = db.session.scalar(select(Dispute).where(Dispute.order_id == order.id))
    assert dispute is not None
    client.get(f"/my/disputes/{dispute.id}")
    client.post("/auth/logout", data={"csrf_token": token})

    login_client(client, other)
    assert client.get(f"/my/disputes/{dispute.id}").status_code == 404
    client.post("/auth/logout", data={"csrf_token": token})

    login_client(client, seller)
    assert client.get(f"/my/disputes/{dispute.id}").status_code == 200
    client.post("/auth/logout", data={"csrf_token": token})

    login_client(client, admin)
    admin_page = client.get(f"/admin/disputes/{dispute.id}")
    token = re.search(rb'name="csrf_token"[^>]*value="([^\"]+)', admin_page.data).group(1).decode()
    rejected = client.post(
        f"/admin/disputes/{dispute.id}/actions/start_review",
        data={"action": "start_review"},
    )
    assert rejected.status_code == 400
    accepted = client.post(
        f"/admin/disputes/{dispute.id}/actions/start_review",
        data={"csrf_token": token, "action": "start_review"},
    )
    assert accepted.status_code in {302, 303}
