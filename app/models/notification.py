"""Persisted, user-scoped application notifications."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import User, utcnow


class NotificationType(StrEnum):
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    OFFER_RECEIVED = "OFFER_RECEIVED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_REJECTED = "OFFER_REJECTED"
    OFFER_COUNTERED = "OFFER_COUNTERED"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_PROCESSING = "ORDER_PROCESSING"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    ORDER_READY_FOR_PICKUP = "ORDER_READY_FOR_PICKUP"
    ORDER_IN_TRANSIT = "ORDER_IN_TRANSIT"
    ORDER_DELIVERED = "ORDER_DELIVERED"
    ORDER_COMPLETED = "ORDER_COMPLETED"
    REVIEW_RECEIVED = "REVIEW_RECEIVED"
    DISPUTE_OPENED = "DISPUTE_OPENED"
    DISPUTE_UNDER_REVIEW = "DISPUTE_UNDER_REVIEW"
    DISPUTE_RESOLVED_FOR_BUYER = "DISPUTE_RESOLVED_FOR_BUYER"
    DISPUTE_RESOLVED_FOR_SELLER = "DISPUTE_RESOLVED_FOR_SELLER"
    REFUND_SUCCEEDED = "REFUND_SUCCEEDED"
    REFUND_FAILED = "REFUND_FAILED"
    LISTING_FLAGGED = "LISTING_FLAGGED"
    LISTING_REMOVED = "LISTING_REMOVED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    ACCOUNT_REACTIVATED = "ACCOUNT_REACTIVATED"


class Notification(db.Model):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "notification_type IN ("
            "'MESSAGE_RECEIVED','OFFER_RECEIVED','OFFER_ACCEPTED','OFFER_REJECTED',"
            "'OFFER_COUNTERED','ORDER_CONFIRMED','ORDER_CANCELLED','ORDER_PROCESSING',"
            "'PAYMENT_SUCCEEDED',"
            "'PAYMENT_FAILED','ORDER_READY_FOR_PICKUP','ORDER_IN_TRANSIT','ORDER_DELIVERED',"
            "'ORDER_COMPLETED','REVIEW_RECEIVED','DISPUTE_OPENED','DISPUTE_UNDER_REVIEW',"
            "'DISPUTE_RESOLVED_FOR_BUYER','DISPUTE_RESOLVED_FOR_SELLER','REFUND_SUCCEEDED',"
            "'REFUND_FAILED','LISTING_FLAGGED','LISTING_REMOVED','ACCOUNT_SUSPENDED',"
            "'ACCOUNT_REACTIVATED')",
            name="ck_notifications_type",
        ),
        CheckConstraint(
            "(target_type IS NULL AND target_id IS NULL) OR "
            "(target_type IN ('conversation','order','listing','seller') "
            "AND target_id > 0)",
            name="ck_notifications_target",
        ),
        CheckConstraint(
            "(is_read = 0 AND read_at IS NULL) OR (is_read = 1 AND read_at IS NOT NULL)",
            name="ck_notifications_read_timestamp",
        ),
        CheckConstraint("length(trim(title)) BETWEEN 1 AND 120", name="ck_notifications_title"),
        CheckConstraint("length(trim(body)) BETWEEN 1 AND 500", name="ck_notifications_body"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_user_read_created", "user_id", "is_read", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(20))
    target_id: Mapped[int | None] = mapped_column()
    is_read: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
