"""Dispute creation, authorization, admin state machine, and service integrations."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import has_request_context
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db, limiter
from app.models import Dispute, DisputeHistory, DisputeReason, DisputeStatus, Order, User
from app.orders.services import OrderService

ELIGIBLE_ORDER_STATES = {"PAID", "PROCESSING", "READY_FOR_PICKUP", "IN_TRANSIT", "DELIVERED"}


class DisputeValidationError(ValueError):
    """Invalid dispute or resolution input."""


def _now() -> datetime:
    return datetime.now(UTC)


def _verified_participant(actor: User) -> None:
    if (
        not actor
        or not actor.is_authenticated
        or actor.phone_verified_at is None
        or actor.role not in {"BUYER", "FARMER"}
    ):
        raise NotFound()


def _admin(actor: User) -> None:
    if (
        not actor
        or not actor.is_authenticated
        or actor.phone_verified_at is None
        or actor.role != "ADMIN"
    ):
        raise NotFound()


def _user_rate_limit(actor: User):
    if not has_request_context():
        return None
    context = limiter.shared_limit(
        "3 per day", scope="dispute-open", key_func=lambda: f"dispute-user:{actor.id}"
    )
    context.__enter__()
    return context


def _plain_text(value, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DisputeValidationError(f"Enter a {label}.")
    clean = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean or len(clean) > limit or "<" in clean or ">" in clean:
        raise DisputeValidationError(
            f"{label.title()} must be plain text and at most {limit} characters."
        )
    return clean


def create_dispute(actor: User, order_id: int, reason, description: str) -> Dispute:
    _verified_participant(actor)
    order = OrderService._owned(actor, order_id)
    if order.status not in ELIGIBLE_ORDER_STATES:
        raise Conflict("This order is not eligible for a dispute.")
    try:
        normalized_reason = DisputeReason(str(reason)).value
    except ValueError as exc:
        raise DisputeValidationError("Choose a supported dispute reason.") from exc
    clean_description = _plain_text(description, label="description", limit=4000)
    if db.session.scalar(select(Dispute.id).where(Dispute.order_id == order.id)):
        raise Conflict("A dispute has already been opened for this order.")
    rate_context = _user_rate_limit(actor)
    try:
        dispute = Dispute(
            order_id=order.id,
            opened_by=actor.id,
            reason=normalized_reason,
            description=clean_description,
            status=DisputeStatus.OPEN.value,
        )
        db.session.add(dispute)
        db.session.flush()
        OrderService._mark_disputed(actor, order.id)
        db.session.add(
            DisputeHistory(
                dispute_id=dispute.id,
                from_status=None,
                to_status=DisputeStatus.OPEN.value,
                actor_id=actor.id,
                note="Dispute opened.",
            )
        )
        db.session.commit()
        return dispute
    except IntegrityError as exc:
        db.session.rollback()
        raise Conflict("A dispute has already been opened for this order.") from exc
    except Exception:
        db.session.rollback()
        raise
    finally:
        if rate_context:
            rate_context.__exit__(None, None, None)


def get_for_order(actor: User, order_id: int) -> Dispute | None:
    _verified_participant(actor)
    owner = Order.buyer_id if actor.role == "BUYER" else Order.seller_id
    order = db.session.scalar(select(Order.id).where(Order.id == order_id, owner == actor.id))
    if order is None:
        raise NotFound()
    return db.session.scalar(
        select(Dispute)
        .where(Dispute.order_id == order_id)
        .options(selectinload(Dispute.history), joinedload(Dispute.opener))
    )


def get_for_participant(actor: User, dispute_id: int) -> Dispute:
    _verified_participant(actor)
    owner = Order.buyer_id if actor.role == "BUYER" else Order.seller_id
    dispute = db.session.scalar(
        select(Dispute)
        .join(Order, Dispute.order_id == Order.id)
        .where(Dispute.id == dispute_id, owner == actor.id)
        .options(joinedload(Dispute.order), selectinload(Dispute.history))
    )
    if dispute is None:
        raise NotFound()
    return dispute


def list_for_admin(actor: User, *, page: int = 1, per_page: int = 20):
    _admin(actor)
    stmt = (
        select(Dispute)
        .where(Dispute.status.in_([DisputeStatus.OPEN.value, DisputeStatus.UNDER_REVIEW.value]))
        .options(joinedload(Dispute.order))
        .order_by(Dispute.opened_at.asc(), Dispute.id.asc())
    )
    return db.paginate(stmt, page=max(page, 1), per_page=min(max(per_page, 1), 50), error_out=False)


def get_for_admin(actor: User, dispute_id: int) -> Dispute:
    _admin(actor)
    dispute = db.session.scalar(
        select(Dispute)
        .where(Dispute.id == dispute_id)
        .options(
            joinedload(Dispute.order),
            joinedload(Dispute.opener),
            selectinload(Dispute.history).joinedload(DisputeHistory.actor),
        )
    )
    if dispute is None:
        raise NotFound()
    return dispute


def admin_transition(actor: User, dispute_id: int, action: str, note: str = "") -> Dispute:
    _admin(actor)
    dispute = db.session.scalar(select(Dispute).where(Dispute.id == dispute_id))
    if dispute is None:
        raise NotFound()
    if dispute.status in {
        DisputeStatus.RESOLVED_FOR_BUYER.value,
        DisputeStatus.RESOLVED_FOR_SELLER.value,
    }:
        raise Conflict("Resolved disputes are closed.")
    if dispute.resolution_pending:
        raise Conflict("A provider refund is still pending for this dispute.")
    clean_note = ""
    if action in {"resolve_for_buyer", "resolve_for_seller"}:
        clean_note = _plain_text(note, label="resolution note", limit=2000)
    elif note:
        clean_note = _plain_text(note, label="admin note", limit=2000)

    if action == "start_review":
        if dispute.status != DisputeStatus.OPEN.value:
            raise Conflict("Only open disputes can move under review.")
        changed = db.session.execute(
            update(Dispute)
            .where(Dispute.id == dispute.id, Dispute.status == DisputeStatus.OPEN.value)
            .values(status=DisputeStatus.UNDER_REVIEW.value, updated_at=_now())
        )
        if changed.rowcount != 1:
            db.session.rollback()
            raise Conflict("The dispute changed; reload and try again.")
        db.session.add(
            DisputeHistory(
                dispute_id=dispute.id,
                from_status=DisputeStatus.OPEN.value,
                to_status=DisputeStatus.UNDER_REVIEW.value,
                actor_id=actor.id,
                note=clean_note or None,
            )
        )
    elif action == "resolve_for_seller":
        if dispute.status != DisputeStatus.UNDER_REVIEW.value:
            raise Conflict("Move the dispute under review before resolving it.")
        order = OrderService._complete_disputed_for_seller(actor, dispute.order_id)
        _ = order
        changed = db.session.execute(
            update(Dispute)
            .where(
                Dispute.id == dispute.id,
                Dispute.status == DisputeStatus.UNDER_REVIEW.value,
                Dispute.resolution_pending.is_(False),
            )
            .values(
                status=DisputeStatus.RESOLVED_FOR_SELLER.value,
                resolution_target=DisputeStatus.RESOLVED_FOR_SELLER.value,
                resolution_note=clean_note,
                resolved_by=actor.id,
                resolved_at=_now(),
                updated_at=_now(),
            )
        )
        if changed.rowcount != 1:
            db.session.rollback()
            raise Conflict("The dispute changed; reload and try again.")
        db.session.add(
            DisputeHistory(
                dispute_id=dispute.id,
                from_status=DisputeStatus.UNDER_REVIEW.value,
                to_status=DisputeStatus.RESOLVED_FOR_SELLER.value,
                actor_id=actor.id,
                note=clean_note,
            )
        )
    elif action == "resolve_for_buyer":
        if dispute.status != DisputeStatus.UNDER_REVIEW.value:
            raise Conflict("Move the dispute under review before resolving it.")
        changed = db.session.execute(
            update(Dispute)
            .where(
                Dispute.id == dispute.id,
                Dispute.status == DisputeStatus.UNDER_REVIEW.value,
                Dispute.resolution_pending.is_(False),
            )
            .values(
                resolution_target=DisputeStatus.RESOLVED_FOR_BUYER.value,
                resolution_note=clean_note,
                resolution_requested_by=actor.id,
                resolution_pending=True,
                updated_at=_now(),
            )
        )
        if changed.rowcount != 1:
            db.session.rollback()
            raise Conflict("The dispute changed; reload and try again.")
        db.session.add(
            DisputeHistory(
                dispute_id=dispute.id,
                from_status=DisputeStatus.UNDER_REVIEW.value,
                to_status=DisputeStatus.UNDER_REVIEW.value,
                actor_id=actor.id,
                note=f"Buyer resolution requested; provider refund pending. {clean_note}",
            )
        )
        db.session.commit()
        from app.payments.providers import PaymentProviderError
        from app.payments.services import PaymentService

        try:
            PaymentService.refund_order(actor, dispute.order_id)
        except (Conflict, NotFound, ValueError, PaymentProviderError) as exc:
            _refund_failed(dispute.order_id, str(exc))
            raise Conflict(
                "The buyer refund could not be started; the dispute remains under review."
            ) from exc
        return db.session.get(Dispute, dispute.id)
    else:
        raise DisputeValidationError("Choose a supported dispute action.")

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return db.session.get(Dispute, dispute.id)


def _refund_failed(order_id: int, failure: str | None = None) -> None:
    dispute = db.session.scalar(select(Dispute).where(Dispute.order_id == order_id))
    if dispute is None or not dispute.resolution_pending:
        return
    dispute.resolution_pending = False
    dispute.updated_at = _now()
    db.session.add(
        DisputeHistory(
            dispute_id=dispute.id,
            from_status=dispute.status,
            to_status=dispute.status,
            actor_id=dispute.resolution_requested_by,
            note=f"Provider refund failed; resolution remains under review. {failure or ''}"[:2000],
        )
    )
    db.session.commit()


def _finalize_buyer_resolution(order_id: int) -> None:
    dispute = db.session.scalar(
        select(Dispute).where(
            Dispute.order_id == order_id,
            Dispute.status == DisputeStatus.UNDER_REVIEW.value,
            Dispute.resolution_pending.is_(True),
            Dispute.resolution_target == DisputeStatus.RESOLVED_FOR_BUYER.value,
        )
    )
    if dispute is None:
        return
    actor_id = dispute.resolution_requested_by
    target = DisputeStatus.RESOLVED_FOR_BUYER.value
    changed = db.session.execute(
        update(Dispute)
        .where(
            Dispute.id == dispute.id,
            Dispute.status == DisputeStatus.UNDER_REVIEW.value,
            Dispute.resolution_pending.is_(True),
        )
        .values(
            status=target,
            resolution_pending=False,
            resolved_by=actor_id,
            resolved_at=_now(),
            updated_at=_now(),
        )
    )
    if changed.rowcount != 1:
        raise Conflict("Dispute resolution changed while refund was confirmed.")
    db.session.add(
        DisputeHistory(
            dispute_id=dispute.id,
            from_status=DisputeStatus.UNDER_REVIEW.value,
            to_status=target,
            actor_id=actor_id,
            note=dispute.resolution_note,
        )
    )
    db.session.flush()


# Internal hook used by PaymentService on a confirmed refund failure, inside that transaction.
def _refund_failure_hook(order_id: int, failure: str | None = None) -> None:
    dispute = db.session.scalar(select(Dispute).where(Dispute.order_id == order_id))
    if dispute is None or not dispute.resolution_pending:
        return
    dispute.resolution_pending = False
    dispute.updated_at = _now()
    db.session.add(
        DisputeHistory(
            dispute_id=dispute.id,
            from_status=dispute.status,
            to_status=dispute.status,
            actor_id=dispute.resolution_requested_by,
            note=f"Provider refund failed; resolution remains under review. {failure or ''}"[:2000],
        )
    )


class DisputeService:
    """Public service API and trusted hooks for the order/payment boundaries."""

    create_dispute = staticmethod(create_dispute)
    get_for_order = staticmethod(get_for_order)
    get_for_participant = staticmethod(get_for_participant)
    list_for_admin = staticmethod(list_for_admin)
    get_for_admin = staticmethod(get_for_admin)
    admin_transition = staticmethod(admin_transition)
    _finalize_buyer_resolution = staticmethod(_finalize_buyer_resolution)
    _refund_failed = staticmethod(_refund_failure_hook)
