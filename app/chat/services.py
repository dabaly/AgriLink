"""Authorization, persistence, and transactional rules for chat and offers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import has_request_context
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db, limiter
from app.marketplace.services import get_public_listing
from app.models import Listing, User
from app.models.marketplace import ListingStatus, ModerationStatus
from app.models.trading import Conversation, Message, Offer, Order, OrderItem, OrderStatusHistory

OFFER_TTL = timedelta(hours=48)
MAX_TOTAL_MINOR = 2_147_483_647


class ChatValidationError(ValueError):
    """Invalid chat or offer input."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _require_participant(actor: User, conversation_id: int) -> Conversation:
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        raise NotFound()
    conv = db.session.scalar(
        select(Conversation)
        .join(Listing, Conversation.listing_id == Listing.id)
        .where(
            Conversation.id == conversation_id,
            or_(Conversation.buyer_id == actor.id, Listing.seller_id == actor.id),
            or_(Conversation.buyer_id != actor.id, actor.role == "BUYER"),
            or_(Listing.seller_id != actor.id, actor.role == "FARMER"),
        )
    )
    if conv is None:
        raise NotFound()
    return conv


def _expire_conversation_offers(conversation_id: int, now: datetime | None = None) -> int:
    now = now or _now()
    result = db.session.execute(
        update(Offer)
        .where(
            Offer.conversation_id == conversation_id,
            Offer.status == "PENDING",
            Offer.expires_at <= now,
        )
        .values(status="EXPIRED", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def expire_pending_offers(now: datetime | None = None) -> int:
    now = now or _now()
    result = db.session.execute(
        update(Offer)
        .where(Offer.status == "PENDING", Offer.expires_at <= now)
        .values(status="EXPIRED", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount or 0


def open_conversation(actor: User, listing_id: int) -> Conversation:
    if (
        not actor
        or not actor.is_authenticated
        or actor.role != "BUYER"
        or actor.phone_verified_at is None
    ):
        raise NotFound()
    existing = db.session.scalar(
        select(Conversation).where(
            Conversation.listing_id == listing_id, Conversation.buyer_id == actor.id
        )
    )
    if existing:
        return existing
    listing = get_public_listing(listing_id)
    if listing.seller_id == actor.id:
        raise NotFound()
    conv = Conversation(listing_id=listing.id, buyer_id=actor.id)
    db.session.add(conv)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = db.session.scalar(
            select(Conversation).where(
                Conversation.listing_id == listing_id, Conversation.buyer_id == actor.id
            )
        )
        if not existing:
            raise
        return existing
    return conv


def list_conversations(actor: User, page: int = 1, per_page: int = 20):
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        raise NotFound()
    if actor.role == "BUYER":
        stmt = select(Conversation).where(Conversation.buyer_id == actor.id)
    elif actor.role == "FARMER":
        stmt = select(Conversation).join(Listing).where(Listing.seller_id == actor.id)
    else:
        raise NotFound()
    stmt = stmt.order_by(
        func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
        Conversation.id.desc(),
    )
    return db.paginate(stmt, page=page, per_page=min(max(per_page, 1), 50), error_out=False)


def get_conversation(actor: User, conversation_id: int) -> Conversation:
    return _require_participant(actor, conversation_id)


def message_history(actor: User, conversation_id: int, page: int = 1, per_page: int = 40):
    conv = _require_participant(actor, conversation_id)
    result = db.paginate(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc()),
        page=page,
        per_page=min(max(per_page, 1), 100),
        error_out=False,
    )
    result.items.reverse()
    return result


def _rate_limit(actor: User):
    if not has_request_context():
        return None
    ctx = limiter.shared_limit(
        "12 per minute",
        scope="chat-message-send",
        key_func=lambda: f"chat-user:{actor.id}",
    )
    ctx.__enter__()
    return ctx


def send_message(actor: User, conversation_id: int, body: str) -> Message:
    conv = _require_participant(actor, conversation_id)
    if not isinstance(body, str):
        raise ChatValidationError("Enter a message.")
    clean = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean or len(clean) > 2000 or "<" in clean or ">" in clean:
        raise ChatValidationError("Messages must be plain text between 1 and 2,000 characters.")
    rate_context = _rate_limit(actor)
    try:
        message = Message(conversation_id=conv.id, sender_id=actor.id, body=clean)
        now = _now()
        conv.last_message_at = now
        conv.updated_at = now
        db.session.add(message)
        db.session.commit()
        return message
    finally:
        if rate_context:
            rate_context.__exit__(None, None, None)


def mark_read(actor: User, conversation_id: int) -> int:
    conv = _require_participant(actor, conversation_id)
    result = db.session.execute(
        update(Message)
        .where(
            Message.conversation_id == conv.id,
            Message.sender_id != actor.id,
            Message.read_at.is_(None),
        )
        .values(read_at=_now())
    )
    db.session.commit()
    return result.rowcount or 0


def unread_count(actor: User) -> int:
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        return 0
    conv_stmt = select(Conversation.id)
    if actor.role == "BUYER":
        conv_stmt = conv_stmt.where(Conversation.buyer_id == actor.id)
    elif actor.role == "FARMER":
        conv_stmt = conv_stmt.join(Listing).where(Listing.seller_id == actor.id)
    else:
        return 0
    return (
        db.session.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id.in_(conv_stmt),
                Message.sender_id != actor.id,
                Message.read_at.is_(None),
            )
        )
        or 0
    )


def _validated_offer(
    actor: User, conv: Conversation, quantity, unit_price_minor
) -> tuple[int, int, int, str]:
    listing = db.session.get(Listing, conv.listing_id)
    if (
        not listing
        or listing.status != ListingStatus.AVAILABLE.value
        or listing.moderation_status != ModerationStatus.APPROVED.value
    ):
        raise Conflict("This listing is no longer available for offers.")
    if isinstance(quantity, bool) or not str(quantity).isascii() or not str(quantity).isdecimal():
        raise ChatValidationError("Quantity must be a whole number.")
    qty = int(quantity)
    if qty <= 0 or qty > listing.quantity:
        raise ChatValidationError("Offer quantity must be within current listing availability.")
    if (
        isinstance(unit_price_minor, bool)
        or not isinstance(unit_price_minor, int)
        or unit_price_minor <= 0
    ):
        raise ChatValidationError("Enter a valid positive KES unit price.")
    total = qty * unit_price_minor
    if total > MAX_TOTAL_MINOR:
        raise ChatValidationError("Offer total is outside the supported range.")
    if actor.role not in ("BUYER", "FARMER"):
        raise NotFound()
    return qty, unit_price_minor, total, listing.unit


def create_offer(
    actor: User,
    conversation_id: int,
    quantity,
    unit_price_minor: int,
    parent_offer_id: int | None = None,
) -> Offer:
    conv = _require_participant(actor, conversation_id)
    _expire_conversation_offers(conv.id)
    vals = _validated_offer(actor, conv, quantity, unit_price_minor)
    if parent_offer_id is not None:
        parent = db.session.scalar(
            select(Offer).where(
                Offer.id == parent_offer_id,
                Offer.conversation_id == conv.id,
                Offer.status == "PENDING",
            )
        )
        if parent is None or parent.proposer_id == actor.id:
            raise NotFound()
        parent.status = "COUNTERED"
    offer = Offer(
        conversation_id=conv.id,
        proposer_id=actor.id,
        parent_offer_id=parent_offer_id,
        quantity=vals[0],
        unit=vals[3],
        unit_price_minor=vals[1],
        total_minor=vals[2],
        status="PENDING",
        expires_at=_now() + OFFER_TTL,
    )
    db.session.add(offer)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise Conflict("There is already a pending offer in this conversation.") from exc
    return offer


def list_offers(actor: User, conversation_id: int) -> list[Offer]:
    conv = _require_participant(actor, conversation_id)
    _expire_conversation_offers(conv.id)
    db.session.commit()
    return list(
        db.session.scalars(
            select(Offer)
            .where(Offer.conversation_id == conv.id)
            .order_by(Offer.created_at.asc(), Offer.id.asc())
        )
    )


def transition_offer(
    actor: User,
    conversation_id: int,
    offer_id: int,
    action: str,
    *,
    counter_quantity=None,
    counter_unit_price_minor=None,
):
    conv = _require_participant(actor, conversation_id)
    _expire_conversation_offers(conv.id)
    offer = db.session.scalar(
        select(Offer).where(Offer.id == offer_id, Offer.conversation_id == conv.id)
    )
    if offer is None:
        raise NotFound()
    now = _now()
    if offer.status != "PENDING" or _aware(offer.expires_at) <= now:
        if offer.status == "PENDING":
            offer.status = "EXPIRED"
            db.session.commit()
        raise Conflict("This offer is no longer pending.")
    if action == "cancel":
        if offer.proposer_id != actor.id:
            raise NotFound()
        offer.status = "CANCELLED"
        db.session.commit()
        return offer, None
    if offer.proposer_id == actor.id:
        raise NotFound()
    if action == "reject":
        offer.status = "REJECTED"
        db.session.commit()
        return offer, None
    if action == "counter":
        new_offer = create_offer(
            actor, conv.id, counter_quantity, counter_unit_price_minor, parent_offer_id=offer.id
        )
        return offer, new_offer
    if action != "accept":
        raise ChatValidationError("Choose a supported offer action.")
    listing = db.session.get(Listing, conv.listing_id)
    if not listing or offer.quantity > listing.quantity or offer.unit != listing.unit:
        raise Conflict("The listing no longer has enough available quantity.")
    # Claim the one pending offer and reserve its stock in the same transaction.
    claimed = db.session.execute(
        update(Offer)
        .where(
            Offer.id == offer.id,
            Offer.conversation_id == conv.id,
            Offer.status == "PENDING",
            Offer.expires_at > now,
            Offer.proposer_id != actor.id,
        )
        .values(status="ACCEPTED", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if not claimed.rowcount:
        db.session.rollback()
        raise Conflict("This offer is no longer pending.")
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
                (Listing.quantity == offer.quantity, ListingStatus.SOLD.value), else_=Listing.status
            ),
            updated_at=now,
        )
    )
    if not stock.rowcount:
        db.session.rollback()
        raise Conflict("The listing is no longer available in that quantity.")
    seller_id = listing.seller_id
    buyer_id = conv.buyer_id
    order = Order(
        accepted_offer_id=offer.id,
        listing_id=listing.id,
        buyer_id=buyer_id,
        seller_id=seller_id,
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
    order.history.append(OrderStatusHistory(status="CONFIRMED", actor_id=actor.id))
    db.session.add(order)
    db.session.execute(
        update(Offer)
        .where(Offer.conversation_id == conv.id, Offer.id != offer.id, Offer.status == "PENDING")
        .values(status="CANCELLED", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return offer, order
