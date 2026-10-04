"""User-scoped notification persistence and post-commit Socket.IO delivery."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from flask import current_app
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session
from werkzeug.exceptions import NotFound

from app.extensions import db, socketio
from app.models import Notification, NotificationType, User


def _now() -> datetime:
    return datetime.now(UTC)


def _verified_user(actor: User) -> None:
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        raise NotFound()


def _after_commit(session: Session) -> None:
    pending = session.info.pop("agri_link_notifications", [])
    for app, user_id, payload in pending:
        try:
            with app.app_context():
                socketio.emit("notification:new", payload, to=f"user:{user_id}")
        except Exception:
            app.logger.exception("Notification real-time delivery failed")


def _after_rollback(session: Session) -> None:
    session.info.pop("agri_link_notifications", None)


event.listen(Session, "after_commit", _after_commit)
event.listen(Session, "after_rollback", _after_rollback)


def create_notification(
    user_id: int,
    notification_type: NotificationType | str,
    title: str,
    body: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
) -> Notification:
    """Add a server-authored notification to the current business transaction."""
    try:
        kind = NotificationType(notification_type).value
    except ValueError as exc:
        raise ValueError("Unsupported notification type.") from exc
    allowed_targets = {None, "conversation", "order", "listing", "seller"}
    if target_type not in allowed_targets or (target_type is None) != (target_id is None):
        raise ValueError("Choose a supported notification target.")
    title = " ".join(str(title).split())
    body = " ".join(str(body).split())
    if not title or len(title) > 120 or not body or len(body) > 500:
        raise ValueError("Notification text is outside the supported length.")
    notification = Notification(
        user_id=user_id,
        notification_type=kind,
        title=title,
        body=body,
        target_type=target_type,
        target_id=target_id,
        created_at=_now(),
    )
    db.session.add(notification)
    payload = {
        "event_id": secrets.token_urlsafe(12),
        "notification_type": kind,
        "title": title,
        "body": body,
        "created_at": notification.created_at.isoformat(),
        "unread_count_delta": 1,
    }
    db.session.info.setdefault("agri_link_notifications", []).append(
        (current_app._get_current_object(), user_id, payload)
    )
    return notification


def list_for_user(actor: User, *, page: int = 1, per_page: int = 20):
    _verified_user(actor)
    return db.paginate(
        select(Notification)
        .where(Notification.user_id == actor.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc()),
        page=max(1, page),
        per_page=min(max(per_page, 1), 50),
        error_out=False,
    )


def get_for_user(actor: User, notification_id: int) -> Notification:
    _verified_user(actor)
    notification = db.session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == actor.id,
        )
    )
    if notification is None:
        raise NotFound()
    return notification


def unread_count(actor: User) -> int:
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        return 0
    return (
        db.session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == actor.id, Notification.is_read.is_(False)
            )
        )
        or 0
    )


def mark_read(actor: User, notification_id: int) -> Notification:
    notification = get_for_user(actor, notification_id)
    if not notification.is_read:
        now = _now()
        db.session.execute(
            update(Notification)
            .where(
                Notification.id == notification.id,
                Notification.user_id == actor.id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=now)
        )
        db.session.commit()
        db.session.refresh(notification)
    return notification


def mark_all_read(actor: User) -> int:
    _verified_user(actor)
    result = db.session.execute(
        update(Notification)
        .where(Notification.user_id == actor.id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=_now())
    )
    db.session.commit()
    return result.rowcount or 0


def target_for_navigation(actor: User, notification_id: int) -> tuple[str | None, int | None]:
    """Return only an existing, participant-accessible target for this user."""
    notification = get_for_user(actor, notification_id)
    target_type, target_id = notification.target_type, notification.target_id
    try:
        if target_type == "conversation":
            from app.chat.services import get_conversation

            get_conversation(actor, target_id)
        elif target_type == "order":
            from app.orders.services import OrderService

            OrderService.get_order(actor, target_id)
        elif target_type == "listing":
            from app.marketplace.services import get_public_listing

            get_public_listing(target_id)
        elif target_type == "seller":
            from app.marketplace.services import get_public_seller

            get_public_seller(target_id)
    except NotFound:
        return None, None
    return target_type, target_id


class NotificationService:
    """Service API; business services stage notifications before committing."""

    create_notification = staticmethod(create_notification)
    list_for_user = staticmethod(list_for_user)
    get_for_user = staticmethod(get_for_user)
    unread_count = staticmethod(unread_count)
    mark_read = staticmethod(mark_read)
    mark_all_read = staticmethod(mark_all_read)
    target_for_navigation = staticmethod(target_for_navigation)
