"""Authoritative order lifecycle, inventory release, and delivery rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, select, update
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db
from app.marketplace.constants import KENYAN_COUNTIES
from app.models import Listing, User
from app.models.marketplace import ListingStatus, ModerationStatus
from app.models.trading import (
    Conversation,
    Delivery,
    Offer,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.utils.phone import normalize_phone

VALID_STATUSES = {
    "PENDING",
    "CONFIRMED",
    "PAID",
    "PROCESSING",
    "READY_FOR_PICKUP",
    "IN_TRANSIT",
    "DELIVERED",
    "COMPLETED",
    "CANCELLED",
    "DISPUTED",
}
AUTO_COMPLETE_AFTER = timedelta(hours=72)


class OrderValidationError(ValueError):
    """Invalid delivery information or order operation."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class OrderService:
    """Only supported entry point for creating and transitioning orders."""

    @staticmethod
    def create_confirmed_from_offer(
        *, actor: User, conversation: Conversation, offer: Offer, listing: Listing
    ) -> Order:
        """Claim listing quantity and create the initial order in caller's transaction."""
        if (
            offer.conversation_id != conversation.id
            or offer.status != "ACCEPTED"
            or offer.proposer_id == actor.id
            or actor.id not in {conversation.buyer_id, listing.seller_id}
            or offer.quantity <= 0
            or offer.unit != listing.unit
            or offer.total_minor != offer.quantity * offer.unit_price_minor
        ):
            raise Conflict("The accepted offer is not valid for this order.")
        now = _now()
        stock = db.session.execute(
            update(Listing)
            .where(
                Listing.id == listing.id,
                Listing.status == ListingStatus.AVAILABLE.value,
                Listing.moderation_status == ModerationStatus.APPROVED.value,
                Listing.quantity >= offer.quantity,
                Listing.category.has(active=True),
                Listing.seller.has(
                    and_(
                        User.role == "FARMER",
                        User.is_active.is_(True),
                        User.is_suspended.is_(False),
                        User.phone_verified_at.is_not(None),
                    )
                ),
            )
            .values(
                quantity=Listing.quantity - offer.quantity,
                status=case(
                    (Listing.quantity == offer.quantity, ListingStatus.SOLD.value),
                    else_=Listing.status,
                ),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not stock.rowcount:
            raise Conflict("The listing is no longer available in that quantity.")

        order = Order(
            accepted_offer_id=offer.id,
            listing_id=listing.id,
            buyer_id=conversation.buyer_id,
            seller_id=listing.seller_id,
            status="CONFIRMED",
            total_minor=offer.total_minor,
        )
        order.item = OrderItem(
            listing_id=listing.id,
            title_snapshot=listing.title,
            quantity=offer.quantity,
            unit=offer.unit,
            unit_price_minor=offer.unit_price_minor,
            total_minor=offer.total_minor,
        )
        order.history.append(
            OrderStatusHistory(previous_status=None, status="CONFIRMED", actor_id=actor.id)
        )
        db.session.add(order)
        return order

    @staticmethod
    def _owned(actor: User, order_id: int) -> Order:
        if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
            raise NotFound()
        if actor.role == "BUYER":
            owner_col = Order.buyer_id
        elif actor.role == "FARMER":
            owner_col = Order.seller_id
        else:
            raise NotFound()
        order = db.session.scalar(select(Order).where(Order.id == order_id, owner_col == actor.id))
        if order is None:
            raise NotFound()
        return order

    @staticmethod
    def list_orders(actor: User, *, page: int = 1, per_page: int = 20, status: str = ""):
        if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
            raise NotFound()
        if actor.role == "BUYER":
            owner_col = Order.buyer_id
        elif actor.role == "FARMER":
            owner_col = Order.seller_id
        else:
            raise NotFound()
        stmt = select(Order).where(owner_col == actor.id)
        if status:
            if status not in VALID_STATUSES:
                status = ""
            else:
                stmt = stmt.where(Order.status == status)
        stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc())
        return db.paginate(stmt, page=page, per_page=min(max(per_page, 1), 50), error_out=False)

    @staticmethod
    def get_order(actor: User, order_id: int) -> Order:
        return OrderService._owned(actor, order_id)

    @staticmethod
    def save_delivery(actor: User, order_id: int, data: dict) -> Delivery:
        order = OrderService._owned(actor, order_id)
        if actor.role != "BUYER":
            raise NotFound()
        if order.status != "CONFIRMED":
            raise Conflict("Fulfillment details are locked after payment or cancellation.")
        method = data.get("method")
        if method not in {"PICKUP", "DELIVERY"}:
            raise OrderValidationError("Choose pickup or delivery.")
        values: dict = {"method": method}
        if method == "DELIVERY":
            county = str(data.get("destination_county", "")).strip()
            location = " ".join(str(data.get("location_name", "")).split())
            address = str(data.get("address_text", "")).strip()
            recipient = " ".join(str(data.get("recipient_name", "")).split())
            phone = str(data.get("recipient_phone", "")).strip()
            if county not in KENYAN_COUNTIES:
                raise OrderValidationError("Choose a valid Kenyan county.")
            if not location or len(location) > 120 or not address or len(address) > 500:
                raise OrderValidationError("Enter a delivery location and address.")
            if not recipient or len(recipient) > 120 or not phone or len(phone) > 40:
                raise OrderValidationError("Enter the recipient name and phone number.")
            try:
                phone = normalize_phone(phone)
            except ValueError as exc:
                raise OrderValidationError("Enter a valid recipient phone number.") from exc
            values.update(
                destination_county=county,
                location_name=location,
                address_text=address,
                recipient_name=recipient,
                recipient_phone=phone,
            )
        delivery = order.delivery
        if delivery is None:
            delivery = Delivery(order_id=order.id, **values)
            db.session.add(delivery)
        else:
            if delivery.status != "PENDING":
                raise Conflict("Delivery method is locked after fulfillment begins.")
            for key, value in values.items():
                setattr(delivery, key, value)
            if method == "PICKUP":
                delivery.destination_county = None
                delivery.location_name = None
                delivery.address_text = None
                delivery.recipient_name = None
                delivery.recipient_phone = None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return delivery

    @staticmethod
    def transition(actor: User, order_id: int, action: str) -> Order:
        order = OrderService._owned(actor, order_id)
        return OrderService._transition(order, action, actor=actor)

    @staticmethod
    def _transition(order: Order, action: str, *, actor: User | None) -> Order:
        old_status = order.status
        delivery = order.delivery
        now = _now()
        new_status: str

        if action == "cancel":
            if old_status != "CONFIRMED" or order.left_seller_at is not None:
                raise Conflict("This order can no longer be cancelled.")
            new_status = "CANCELLED"
        elif action == "start_processing":
            if actor is None or actor.role != "FARMER" or old_status != "PAID":
                raise Conflict("Only paid orders can enter fulfillment.")
            if delivery is None:
                raise Conflict("The buyer must choose pickup or delivery first.")
            new_status = "PROCESSING"
        elif action == "prepare_fulfillment":
            if actor is None or actor.role != "FARMER" or old_status != "PROCESSING":
                raise Conflict("This order is not ready for that fulfillment action.")
            if delivery is None:
                raise Conflict("Delivery instructions are required before fulfillment.")
            new_status = "READY_FOR_PICKUP" if delivery.method == "PICKUP" else "IN_TRANSIT"
        elif action == "mark_delivered":
            if (
                actor is None
                or actor.role != "FARMER"
                or old_status not in {"READY_FOR_PICKUP", "IN_TRANSIT"}
            ):
                raise Conflict("Only a prepared pickup or in-transit order can be delivered.")
            if delivery is None:
                raise Conflict("Delivery instructions are missing.")
            if delivery.method == "PICKUP" and old_status != "READY_FOR_PICKUP":
                raise Conflict("Pickup must be ready before handoff.")
            if delivery.method == "DELIVERY" and old_status != "IN_TRANSIT":
                raise Conflict("Delivery must be in transit before completion.")
            new_status = "DELIVERED"
        elif action == "confirm_completion":
            if actor is None or actor.role != "BUYER" or old_status != "DELIVERED":
                raise Conflict("Only the buyer can complete a delivered order.")
            new_status = "COMPLETED"
        elif action == "complete_after_72h":
            if actor is not None or old_status != "DELIVERED":
                raise Conflict("Only delivered orders can complete automatically.")
            delivered_at = order.delivered_at
            if not delivered_at or _aware(delivered_at) + AUTO_COMPLETE_AFTER > now:
                raise Conflict("The automatic completion window has not elapsed.")
            new_status = "COMPLETED"
        else:
            raise OrderValidationError("Choose a supported order action.")

        changed = db.session.execute(
            update(Order)
            .where(
                Order.id == order.id,
                Order.status == old_status,
                Order.left_seller_at.is_(None) if action == "cancel" else True,
            )
            .values(
                status=new_status,
                updated_at=now,
                cancelled_at=now if new_status == "CANCELLED" else Order.cancelled_at,
                delivered_at=now if new_status == "DELIVERED" else Order.delivered_at,
                completed_at=now if new_status == "COMPLETED" else Order.completed_at,
                left_seller_at=(
                    now
                    if new_status == "IN_TRANSIT"
                    or (new_status == "DELIVERED" and delivery and delivery.method == "PICKUP")
                    else Order.left_seller_at
                ),
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            db.session.rollback()
            raise Conflict("The order changed; reload it and try again.")

        if action == "cancel":
            item = order.item
            listing = db.session.execute(
                update(Listing)
                .where(Listing.id == order.listing_id)
                .values(
                    quantity=Listing.quantity + item.quantity,
                    status=case(
                        (Listing.status == ListingStatus.SOLD.value, ListingStatus.AVAILABLE.value),
                        else_=Listing.status,
                    ),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if listing.rowcount != 1:
                db.session.rollback()
                raise Conflict("Reserved stock could not be released.")
            if delivery:
                delivery.status = "CANCELLED"
        elif action == "start_processing":
            delivery.status = "PREPARING"
        elif action == "prepare_fulfillment":
            delivery.status = "READY" if new_status == "READY_FOR_PICKUP" else "IN_TRANSIT"
            if new_status == "IN_TRANSIT":
                delivery.fulfilled_at = now
        elif action == "mark_delivered":
            delivery.status = "DELIVERED"
            delivery.fulfilled_at = now

        db.session.add(
            OrderStatusHistory(
                order_id=order.id,
                previous_status=old_status,
                status=new_status,
                actor_id=actor.id if actor else None,
                created_at=now,
            )
        )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        db.session.expire(order)
        return order

    @staticmethod
    def complete_delivered_orders(now: datetime | None = None) -> int:
        now = now or _now()
        cutoff = now - AUTO_COMPLETE_AFTER
        ids = list(
            db.session.scalars(
                select(Order.id).where(Order.status == "DELIVERED", Order.delivered_at <= cutoff)
            )
        )
        completed = 0
        for order_id in ids:
            order = db.session.get(Order, order_id)
            if order is None:
                continue
            try:
                OrderService._transition(order, "complete_after_72h", actor=None)
                completed += 1
            except Conflict:
                db.session.rollback()
        return completed
