"""Persistent conversations, offers, and the confirmed-order foundation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import User, utcnow


class Conversation(db.Model):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("listing_id", "buyer_id", name="uq_conversations_listing_buyer"),
        Index("ix_conversations_buyer_activity", "buyer_id", "updated_at"),
        Index("ix_conversations_listing_activity", "listing_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False
    )
    buyer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listing = relationship("Listing")
    buyer: Mapped[User] = relationship(foreign_keys=[buyer_id])
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    offers: Mapped[list[Offer]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(db.Model):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("length(trim(body)) BETWEEN 1 AND 2000", name="ck_messages_body_length"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_unread", "conversation_id", "sender_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    sender: Mapped[User] = relationship(foreign_keys=[sender_id])


class Offer(db.Model):
    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_offers_quantity_positive"),
        CheckConstraint(
            "unit_price_minor > 0 AND total_minor > 0", name="ck_offers_price_positive"
        ),
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED','EXPIRED','CANCELLED','COUNTERED')",
            name="ck_offers_status",
        ),
        Index("ix_offers_conversation_created", "conversation_id", "created_at"),
        Index(
            "uq_offers_one_pending_per_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    proposer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Historical pointer; offer rows are immutable and are never deleted.
    parent_offer_id: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="offers")
    proposer: Mapped[User] = relationship(foreign_keys=[proposer_id])


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','CONFIRMED','PAID','PROCESSING','READY_FOR_PICKUP',"
            "'IN_TRANSIT','DELIVERED','COMPLETED','CANCELLED','DISPUTED')",
            name="ck_orders_status",
        ),
        CheckConstraint("total_minor > 0", name="ck_orders_total_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    accepted_offer_id: Mapped[int] = mapped_column(
        ForeignKey("offers.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    left_seller_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    item: Mapped[OrderItem] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )
    history: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    delivery: Mapped[Delivery | None] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )


class OrderItem(db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_order_items_one_item"),
        CheckConstraint(
            "quantity > 0 AND unit_price_minor > 0 AND total_minor > 0",
            name="ck_order_items_values",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False
    )
    title_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="item")


class OrderStatusHistory(db.Model):
    __tablename__ = "order_status_history"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','CONFIRMED','PAID','PROCESSING','READY_FOR_PICKUP',"
            "'IN_TRANSIT','DELIVERED','COMPLETED','CANCELLED','DISPUTED')",
            name="ck_order_status_history_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    order: Mapped[Order] = relationship(back_populates="history")


@event.listens_for(OrderStatusHistory, "before_update")
@event.listens_for(OrderStatusHistory, "before_delete")
def _keep_order_history_append_only(_mapper, _connection, _target) -> None:
    raise ValueError("Order status history is append-only.")


@event.listens_for(OrderItem, "before_update")
@event.listens_for(OrderItem, "before_delete")
def _keep_order_item_snapshot_immutable(_mapper, _connection, _target) -> None:
    raise ValueError("Order item snapshots are immutable.")


class Delivery(db.Model):
    """Private pickup or destination instructions for one order."""

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint("method IN ('PICKUP','DELIVERY')", name="ck_deliveries_method"),
        CheckConstraint(
            "status IN ('PENDING','PREPARING','READY','IN_TRANSIT','DELIVERED','CANCELLED')",
            name="ck_deliveries_status",
        ),
        CheckConstraint(
            "method != 'DELIVERY' OR (destination_county IS NOT NULL "
            "AND location_name IS NOT NULL AND address_text IS NOT NULL "
            "AND recipient_name IS NOT NULL AND recipient_phone IS NOT NULL)",
            name="ck_deliveries_destination_required",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False)
    destination_county: Mapped[str | None] = mapped_column(String(40))
    location_name: Mapped[str | None] = mapped_column(String(120))
    address_text: Mapped[str | None] = mapped_column(String(500))
    recipient_name: Mapped[str | None] = mapped_column(String(120))
    recipient_phone: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="delivery")
