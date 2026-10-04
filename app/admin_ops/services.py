"""Admin-only operational queries and transactional actions."""

from __future__ import annotations

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db
from app.models import AuditLog, Dispute, Listing, Order, User
from app.models.feedback import DisputeStatus
from app.models.marketplace import ListingStatus, ModerationStatus


def require_admin(actor: User) -> None:
    if (
        not actor
        or not actor.is_authenticated
        or not actor.is_active
        or actor.is_suspended
        or actor.role != "ADMIN"
        or actor.phone_verified_at is None
    ):
        raise NotFound()


def _record(actor: User, action: str, target_type: str, target_id: int, metadata=None):
    from app.audit.services import AuditService

    return AuditService.append(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
    )


def dashboard_counts(actor: User) -> dict[str, int]:
    require_admin(actor)

    def count(model, *conditions):
        return db.session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0

    return {
        "active_users": count(User, User.is_active.is_(True), User.is_suspended.is_(False)),
        "verified_users": count(User, User.phone_verified_at.is_not(None)),
        "active_listings": count(
            Listing,
            Listing.status == ListingStatus.AVAILABLE.value,
            Listing.moderation_status == ModerationStatus.APPROVED.value,
        ),
        "flagged_listings": count(
            Listing, Listing.moderation_status == ModerationStatus.FLAGGED.value
        ),
        "open_disputes": count(Dispute, Dispute.status == DisputeStatus.OPEN.value),
        "disputes_under_review": count(Dispute, Dispute.status == DisputeStatus.UNDER_REVIEW.value),
        "pending_orders": count(Order, Order.status.in_(["PENDING", "CONFIRMED"])),
    }


def list_users(actor: User, *, role="", state="", verified="", page=1):
    require_admin(actor)
    stmt = (
        select(User)
        .options(joinedload(User.profile))
        .order_by(User.created_at.desc(), User.id.desc())
    )
    if role in {"FARMER", "BUYER", "ADMIN"}:
        stmt = stmt.where(User.role == role)
    if state == "active":
        stmt = stmt.where(User.is_active.is_(True), User.is_suspended.is_(False))
    elif state == "suspended":
        stmt = stmt.where(User.is_suspended.is_(True))
    if verified == "yes":
        stmt = stmt.where(User.phone_verified_at.is_not(None))
    elif verified == "no":
        stmt = stmt.where(User.phone_verified_at.is_(None))
    return db.paginate(stmt, page=max(1, page), per_page=25, error_out=False)


def get_user(actor: User, user_id: int) -> User:
    require_admin(actor)
    user = db.session.scalar(
        select(User).where(User.id == user_id).options(joinedload(User.profile))
    )
    if user is None:
        raise NotFound()
    return user


def set_user_suspended(actor: User, user_id: int, *, suspended: bool) -> User:
    require_admin(actor)
    user = db.session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise NotFound()
    if user.role == "ADMIN":
        raise Conflict("Administrator accounts cannot be changed here.")
    if user.is_suspended == suspended:
        raise Conflict("The account already has that status.")
    user.is_suspended = suspended
    action = "USER_SUSPENDED" if suspended else "USER_REACTIVATED"
    try:
        _record(actor, action, "user", user.id, {"role": user.role})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    _best_effort_user_notice(user, suspended)
    return user


def _best_effort_user_notice(user: User, suspended: bool) -> None:
    """Notify after the suspension transaction; notification failure is isolated."""
    from app.models import NotificationType
    from app.notifications.services import NotificationService

    try:
        NotificationService.create_notification(
            user.id,
            NotificationType.ACCOUNT_SUSPENDED
            if suspended
            else NotificationType.ACCOUNT_REACTIVATED,
            "Account access updated",
            "Your AgriLink account has been suspended."
            if suspended
            else "Your AgriLink account is active again.",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def list_listings(actor: User, *, moderation="", status="", category="", page=1):
    require_admin(actor)
    stmt = (
        select(Listing)
        .options(
            joinedload(Listing.seller).joinedload(User.profile),
            joinedload(Listing.category),
            selectinload(Listing.images),
        )
        .order_by(Listing.created_at.desc(), Listing.id.desc())
    )
    if moderation in {item.value for item in ModerationStatus}:
        stmt = stmt.where(Listing.moderation_status == moderation)
    if status in {item.value for item in ListingStatus}:
        stmt = stmt.where(Listing.status == status)
    if category.isdigit():
        stmt = stmt.where(Listing.category_id == int(category))
    return db.paginate(stmt, page=max(1, page), per_page=25, error_out=False)


def get_listing(actor: User, listing_id: int) -> Listing:
    require_admin(actor)
    listing = db.session.scalar(
        select(Listing)
        .where(Listing.id == listing_id)
        .options(
            joinedload(Listing.seller).joinedload(User.profile),
            joinedload(Listing.category),
            selectinload(Listing.images),
        )
    )
    if listing is None:
        raise NotFound()
    return listing


_MODERATION_ACTIONS = {
    "flag": (ModerationStatus.FLAGGED.value, "LISTING_FLAGGED", {"APPROVED"}),
    "approve": (ModerationStatus.APPROVED.value, "LISTING_APPROVED", {"FLAGGED"}),
    "remove": (ModerationStatus.REMOVED.value, "LISTING_REMOVED", {"FLAGGED", "APPROVED"}),
}


def moderate_listing(actor: User, listing_id: int, action: str) -> Listing:
    require_admin(actor)
    listing = db.session.scalar(select(Listing).where(Listing.id == listing_id).with_for_update())
    if listing is None:
        raise NotFound()
    if action not in _MODERATION_ACTIONS:
        raise ValueError("Choose a supported moderation action.")
    target, audit_action, allowed = _MODERATION_ACTIONS[action]
    if listing.moderation_status not in allowed:
        raise Conflict("That moderation transition is not allowed.")
    listing.moderation_status = target
    try:
        _record(actor, audit_action, "listing", listing.id, {"moderation_status": target})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if action in {"flag", "remove"}:
        _best_effort_listing_notice(listing, action)
    return listing


def _best_effort_listing_notice(listing: Listing, action: str) -> None:
    from app.models import NotificationType
    from app.notifications.services import NotificationService

    removed = action == "remove"
    try:
        NotificationService.create_notification(
            listing.seller_id,
            NotificationType.LISTING_REMOVED if removed else NotificationType.LISTING_FLAGGED,
            "Listing moderation update",
            "Your listing was removed from the marketplace."
            if removed
            else "Your listing was flagged for review.",
            target_type="listing",
            target_id=listing.id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Listing moderation notification could not be saved")


def list_audit(actor: User, *, action="", target_type="", page=1):
    require_admin(actor)
    stmt = (
        select(AuditLog)
        .options(joinedload(AuditLog.actor))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    if action in {
        "USER_SUSPENDED",
        "USER_REACTIVATED",
        "LISTING_FLAGGED",
        "LISTING_APPROVED",
        "LISTING_REMOVED",
        "DISPUTE_MOVED_TO_REVIEW",
        "DISPUTE_RESOLVED_FOR_BUYER",
        "DISPUTE_RESOLVED_FOR_SELLER",
    }:
        stmt = stmt.where(AuditLog.action == action)
    if target_type in {"user", "listing", "dispute"}:
        stmt = stmt.where(AuditLog.target_type == target_type)
    return db.paginate(stmt, page=max(1, page), per_page=25, error_out=False)
