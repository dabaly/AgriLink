"""Transaction review eligibility, persistence, and public aggregates."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from flask import has_request_context
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db, limiter
from app.models import Order, Profile, Review, User


class ReviewValidationError(ValueError):
    """Invalid plain-text review input."""


def _verified_buyer(actor: User) -> None:
    if (
        not actor
        or not actor.is_authenticated
        or actor.role != "BUYER"
        or actor.phone_verified_at is None
    ):
        raise NotFound()


def _rate_limit(actor: User):
    if not has_request_context():
        return None
    context = limiter.shared_limit(
        "5 per hour", scope="review-submit", key_func=lambda: f"review-user:{actor.id}"
    )
    context.__enter__()
    return context


def create_review(actor: User, order_id: int, rating, body: str) -> Review:
    _verified_buyer(actor)
    order = db.session.scalar(select(Order).where(Order.id == order_id, Order.buyer_id == actor.id))
    if order is None:
        raise NotFound()
    if order.status != "COMPLETED":
        raise Conflict("A review is available only after an order is completed.")
    if order.buyer_id == order.seller_id:
        raise NotFound()
    if order.item is None or order.item.listing_id != order.listing_id:
        raise Conflict("This order has no eligible purchased listing.")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ReviewValidationError("Choose a whole-number rating from 1 to 5.")
    if not isinstance(body, str):
        raise ReviewValidationError("Enter a review.")
    clean = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean or len(clean) > 2000 or "<" in clean or ">" in clean:
        raise ReviewValidationError("Reviews must be plain text between 1 and 2,000 characters.")
    if db.session.scalar(select(Review.id).where(Review.order_id == order.id)):
        raise Conflict("This completed order already has a review.")
    limiter_context = _rate_limit(actor)
    try:
        review = Review(
            order_id=order.id,
            reviewer_id=actor.id,
            seller_id=order.seller_id,
            listing_id=order.listing_id,
            rating=rating,
            body=clean,
        )
        db.session.add(review)
        db.session.flush()
        total, count = db.session.execute(
            select(func.sum(Review.rating), func.count(Review.id)).where(
                Review.seller_id == order.seller_id
            )
        ).one()
        average = (Decimal(total) / Decimal(count)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        profile = db.session.scalar(select(Profile).where(Profile.user_id == order.seller_id))
        if profile is not None:
            profile.rating_avg = float(average)
            profile.rating_count = count
        db.session.commit()
        return review
    except IntegrityError as exc:
        db.session.rollback()
        raise Conflict("This completed order already has a review.") from exc
    except Exception:
        db.session.rollback()
        raise
    finally:
        if limiter_context:
            limiter_context.__exit__(None, None, None)


def get_order_review(actor: User, order_id: int) -> Review | None:
    if not actor or not actor.is_authenticated or actor.phone_verified_at is None:
        raise NotFound()
    if actor.role == "BUYER":
        owner = Order.buyer_id
    elif actor.role == "FARMER":
        owner = Order.seller_id
    else:
        raise NotFound()
    order = db.session.scalar(select(Order.id).where(Order.id == order_id, owner == actor.id))
    if order is None:
        raise NotFound()
    return db.session.scalar(
        select(Review)
        .where(Review.order_id == order_id)
        .options(joinedload(Review.reviewer).joinedload(User.profile))
    )


def public_reviews_for_listing(listing_id: int, *, limit: int = 5) -> list[Review]:
    return list(
        db.session.scalars(
            select(Review)
            .where(Review.listing_id == listing_id, Review.order.has(Order.status == "COMPLETED"))
            .options(joinedload(Review.reviewer).joinedload(User.profile))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(min(max(limit, 1), 20))
        )
    )


def public_reviews_for_seller(seller_id: int, *, limit: int = 8) -> list[Review]:
    return list(
        db.session.scalars(
            select(Review)
            .where(Review.seller_id == seller_id, Review.order.has(Order.status == "COMPLETED"))
            .options(joinedload(Review.reviewer).joinedload(User.profile))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(min(max(limit, 1), 30))
        )
    )


def seller_review_summary(seller_id: int) -> tuple[Decimal | None, int]:
    total, count = db.session.execute(
        select(func.sum(Review.rating), func.count(Review.id)).where(Review.seller_id == seller_id)
    ).one()
    if not count:
        return None, 0
    average = (Decimal(total) / Decimal(count)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return average, count


class ReviewService:
    """Public service API for review creation and safe review queries."""

    create_review = staticmethod(create_review)
    get_order_review = staticmethod(get_order_review)
    public_reviews_for_listing = staticmethod(public_reviews_for_listing)
    public_reviews_for_seller = staticmethod(public_reviews_for_seller)
    seller_review_summary = staticmethod(seller_review_summary)
