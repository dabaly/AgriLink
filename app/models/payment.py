"""Payment and idempotent provider event persistence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import utcnow


class PaymentStatus(StrEnum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REFUNDED = "REFUNDED"


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("provider IN ('MOCK','MPESA','STRIPE')", name="ck_payments_provider"),
        CheckConstraint("currency = 'KES'", name="ck_payments_currency_kes"),
        CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "status IN ('INITIATED','PENDING','SUCCEEDED','FAILED','EXPIRED','REFUNDED')",
            name="ck_payments_status",
        ),
        UniqueConstraint("provider", "provider_reference", name="uq_payments_provider_reference"),
        Index("ix_payments_order_created", "order_id", "created_at"),
        Index(
            "uq_payments_one_success_per_order",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('SUCCEEDED','REFUNDED')"),
            postgresql_where=text("status IN ('SUCCEEDED','REFUNDED')"),
        ),
        Index(
            "uq_payments_one_active_per_order",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('INITIATED','PENDING')"),
            postgresql_where=text("status IN ('INITIATED','PENDING')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(12), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(255))
    provider_resource_id: Mapped[str | None] = mapped_column(String(255))
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KES", nullable=False)
    status: Mapped[str] = mapped_column(String(18), default=PaymentStatus.INITIATED, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    refund_reference: Mapped[str | None] = mapped_column(String(255))
    refund_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    order = relationship("Order")
    events: Mapped[list[PaymentEvent]] = relationship(back_populates="payment")


class PaymentEvent(db.Model):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_events_provider_event"),
        CheckConstraint(
            "processing_status IN ('PROCESSED','DUPLICATE','REJECTED','ACTION_REQUIRED')",
            name="ck_payment_events_status",
        ),
        Index("ix_payment_events_payment_received", "payment_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(12), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_message: Mapped[str | None] = mapped_column(String(160))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    payment: Mapped[Payment | None] = relationship(back_populates="events")
