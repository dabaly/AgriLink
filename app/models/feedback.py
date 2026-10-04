"""Transaction reviews and dispute records with append-only state history."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import User, utcnow


class DisputeStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED_FOR_BUYER = "RESOLVED_FOR_BUYER"
    RESOLVED_FOR_SELLER = "RESOLVED_FOR_SELLER"


class DisputeReason(StrEnum):
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED"
    ITEM_NOT_AS_DESCRIBED = "ITEM_NOT_AS_DESCRIBED"
    QUALITY_ISSUE = "QUALITY_ISSUE"
    QUANTITY_ISSUE = "QUANTITY_ISSUE"
    PAYMENT_ISSUE = "PAYMENT_ISSUE"
    DELIVERY_ISSUE = "DELIVERY_ISSUE"
    OTHER = "OTHER"


class Review(db.Model):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating_range"),
        CheckConstraint("length(trim(body)) BETWEEN 1 AND 2000", name="ck_reviews_body_length"),
        UniqueConstraint("order_id", name="uq_reviews_one_per_order"),
        Index("ix_reviews_seller_created", "seller_id", "created_at"),
        Index("ix_reviews_listing_created", "listing_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False
    )
    rating: Mapped[int] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    order = relationship("Order")
    reviewer: Mapped[User] = relationship(foreign_keys=[reviewer_id])
    seller: Mapped[User] = relationship(foreign_keys=[seller_id])
    listing = relationship("Listing")


class Dispute(db.Model):
    __tablename__ = "disputes"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_disputes_one_per_order"),
        CheckConstraint(
            "reason IN ('ITEM_NOT_RECEIVED','ITEM_NOT_AS_DESCRIBED','QUALITY_ISSUE',"
            "'QUANTITY_ISSUE','PAYMENT_ISSUE','DELIVERY_ISSUE','OTHER')",
            name="ck_disputes_reason",
        ),
        CheckConstraint(
            "status IN ('OPEN','UNDER_REVIEW','RESOLVED_FOR_BUYER','RESOLVED_FOR_SELLER')",
            name="ck_disputes_status",
        ),
        CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 4000", name="ck_disputes_description_length"
        ),
        CheckConstraint(
            "resolution_target IS NULL OR resolution_target IN "
            "('RESOLVED_FOR_BUYER','RESOLVED_FOR_SELLER')",
            name="ck_disputes_resolution_target",
        ),
        Index("ix_disputes_status_opened", "status", "opened_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    opened_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default=DisputeStatus.OPEN, nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(String(2000))
    resolution_target: Mapped[str | None] = mapped_column(String(24))
    resolution_pending: Mapped[bool] = mapped_column(default=False, nullable=False)
    resolution_requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order = relationship("Order")
    opener: Mapped[User] = relationship(foreign_keys=[opened_by])
    resolver: Mapped[User | None] = relationship(foreign_keys=[resolved_by])
    requester: Mapped[User | None] = relationship(foreign_keys=[resolution_requested_by])
    history: Mapped[list[DisputeHistory]] = relationship(
        back_populates="dispute", order_by="DisputeHistory.id"
    )


class DisputeHistory(db.Model):
    __tablename__ = "dispute_history"
    __table_args__ = (
        CheckConstraint(
            "to_status IN ('OPEN','UNDER_REVIEW','RESOLVED_FOR_BUYER','RESOLVED_FOR_SELLER')",
            name="ck_dispute_history_to_status",
        ),
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('OPEN','UNDER_REVIEW','RESOLVED_FOR_BUYER','RESOLVED_FOR_SELLER')",
            name="ck_dispute_history_from_status",
        ),
        Index("ix_dispute_history_dispute_created", "dispute_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dispute_id: Mapped[int] = mapped_column(
        ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    note: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    dispute: Mapped[Dispute] = relationship(back_populates="history")
    actor: Mapped[User | None] = relationship()


@event.listens_for(Review, "before_update")
@event.listens_for(Review, "before_delete")
def _keep_reviews_immutable(_mapper, _connection, _target) -> None:
    raise ValueError("Reviews are immutable after creation.")


@event.listens_for(DisputeHistory, "before_update")
@event.listens_for(DisputeHistory, "before_delete")
def _keep_dispute_history_append_only(_mapper, _connection, _target) -> None:
    raise ValueError("Dispute history is append-only.")
