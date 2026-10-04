"""Admin operations authorization, moderation, and append-only audit tests."""

import re

import pytest
from sqlalchemy import select
from werkzeug.exceptions import Conflict, NotFound

from app.admin_ops.services import moderate_listing, set_user_suspended
from app.auth.services import AuthenticationError, authenticate
from app.extensions import db
from app.marketplace.services import seed_categories
from app.models import AuditLog, Listing, Notification
from tests.test_chat import login_client
from tests.test_orders import make_order, make_user


@pytest.fixture
def admin_parties(app):
    seed_categories()
    buyer = make_user("+254711100001", "BUYER")
    seller = make_user("+254722200002", "FARMER")
    admin = make_user("+254733300003", "ADMIN")
    listing, _order = make_order(buyer, seller)
    return buyer, seller, admin, listing


def _csrf(response):
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)', response.data)
    assert match
    return match.group(1).decode()


@pytest.mark.parametrize("role", ["BUYER", "FARMER"])
def test_admin_pages_are_denied_to_anonymous_and_regular_users(admin_parties, app, client, role):
    buyer, seller, _admin, _listing = admin_parties
    user = buyer if role == "BUYER" else seller
    assert client.get("/admin").status_code == 302
    login_client(client, user)
    for path in ("/admin", "/admin/users", "/admin/listings", "/admin/audit", "/admin/disputes"):
        assert client.get(path).status_code == 404


def test_dashboard_and_admin_pages_render(admin_parties, client):
    _buyer, _seller, admin, _listing = admin_parties
    login_client(client, admin)
    assert b"Admin operations" in client.get("/admin").data
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/listings").status_code == 200
    assert client.get("/admin/audit").status_code == 200


def test_listing_moderation_is_transactional_and_transition_limited(admin_parties, app):
    _buyer, seller, admin, listing = admin_parties
    assert moderate_listing(admin, listing.id, "flag").moderation_status == "FLAGGED"
    assert moderate_listing(admin, listing.id, "approve").moderation_status == "APPROVED"
    assert moderate_listing(admin, listing.id, "remove").moderation_status == "REMOVED"
    assert db.session.get(Listing, listing.id) is not None
    assert db.session.scalar(select(db.func.count(AuditLog.id))) == 3
    assert (
        db.session.scalar(
            select(db.func.count(Notification.id)).where(Notification.user_id == seller.id)
        )
        >= 2
    )
    with pytest.raises(Conflict):
        moderate_listing(admin, listing.id, "approve")


def test_admin_listing_actions_require_csrf_and_are_post_only(admin_parties, client):
    _buyer, _seller, admin, listing = admin_parties
    login_client(client, admin)
    assert client.get(f"/admin/listings/{listing.id}/moderation/flag").status_code == 405
    assert client.post(f"/admin/listings/{listing.id}/moderation/flag").status_code == 400
    detail = client.get(f"/admin/listings/{listing.id}")
    response = client.post(
        f"/admin/listings/{listing.id}/moderation/flag", data={"csrf_token": _csrf(detail)}
    )
    assert response.status_code == 302


def test_suspend_reactivate_admin_protected_and_preserves_history(admin_parties):
    _buyer, seller, admin, listing = admin_parties
    set_user_suspended(admin, seller.id, suspended=True)
    assert seller.is_suspended
    assert db.session.get(Listing, listing.id) is not None
    with pytest.raises(AuthenticationError):
        authenticate(seller.phone, "correct-horse-battery-staple")
    with pytest.raises(Conflict):
        set_user_suspended(admin, admin.id, suspended=True)
    with pytest.raises(NotFound):
        set_user_suspended(seller, admin.id, suspended=True)
    set_user_suspended(admin, seller.id, suspended=False)
    assert not seller.is_suspended
    assert [row.action for row in db.session.scalars(select(AuditLog).order_by(AuditLog.id))] == [
        "USER_SUSPENDED",
        "USER_REACTIVATED",
    ]


def test_suspension_notification_failure_does_not_undo_suspension(admin_parties, monkeypatch):
    _buyer, seller, admin, _listing = admin_parties

    def broken(*args, **kwargs):
        raise RuntimeError("notification database failure")

    monkeypatch.setattr(
        "app.notifications.services.NotificationService.create_notification", broken
    )
    set_user_suspended(admin, seller.id, suspended=True)
    db.session.refresh(seller)
    assert seller.is_suspended
    assert db.session.scalar(select(AuditLog.action).where(AuditLog.action == "USER_SUSPENDED"))


def test_audit_is_admin_only_and_orm_append_only(admin_parties, client):
    _buyer, _seller, admin, listing = admin_parties
    moderate_listing(admin, listing.id, "flag")
    login_client(client, admin)
    assert b"LISTING_FLAGGED" in client.get("/admin/audit?action=LISTING_FLAGGED").data
    entry = db.session.scalar(select(AuditLog))
    entry.action = "LISTING_REMOVED"
    with pytest.raises(ValueError, match="append-only"):
        db.session.commit()
    db.session.rollback()
