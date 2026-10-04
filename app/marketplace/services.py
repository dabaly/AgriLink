"""Marketplace rules, access scoping, and query construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import current_app
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import NotFound

from app.extensions import db
from app.marketplace.constants import (
    KENYAN_COUNTIES,
    MAX_IMAGES_PER_LISTING,
    MAX_PAGE_SIZE,
    PAGE_SIZE,
)
from app.marketplace.images import (
    ImageValidationError,
    ProcessedImage,
    process_image,
    remove_processed_files,
    remove_stored_files,
)
from app.models import Category, Listing, ListingImage, User
from app.models.marketplace import ListingStatus, ModerationStatus, QualityGrade, Unit


class MarketplaceValidationError(ValueError):
    """Invalid marketplace input or business rule."""


@dataclass(frozen=True)
class SearchCriteria:
    query: str = ""
    category: str = ""
    county: str = ""
    grade: str = ""
    unit: str = ""
    min_price: int | None = None
    max_price: int | None = None
    sort: str = "newest"
    page: int = 1
    per_page: int = PAGE_SIZE


def seed_categories() -> int:
    """Insert any missing built-in categories and commit once."""
    from app.marketplace.constants import INITIAL_CATEGORIES

    inserted = 0
    try:
        existing = {slug for (slug,) in db.session.execute(select(Category.slug))}
        for name, slug, description in INITIAL_CATEGORIES:
            if slug not in existing:
                db.session.add(Category(name=name, slug=slug, description=description, active=True))
                inserted += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return inserted


def parse_kes_to_minor(value: str) -> int:
    """Convert a strict decimal KES input to integer minor units."""
    if (
        not isinstance(value, str)
        or len(value) > 18
        or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", value.strip())
    ):
        raise MarketplaceValidationError("Enter a valid KES amount with up to two decimals.")
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise MarketplaceValidationError("Enter a valid KES amount.") from exc
    minor = amount * 100
    if not amount.is_finite() or minor != minor.to_integral_value() or minor > 2_147_483_647:
        raise MarketplaceValidationError("KES amount is outside the allowed range.")
    return int(minor)


def format_kes(price_minor: int) -> str:
    return f"KES {price_minor // 100:,}.{price_minor % 100:02d}"


def _require_farmer(actor: User) -> None:
    if not actor or not actor.is_authenticated:
        raise NotFound()
    if actor.role != "FARMER" or actor.phone_verified_at is None:
        raise NotFound()


def _public_seller_criteria():
    return and_(
        User.role == "FARMER",
        User.is_active.is_(True),
        User.is_suspended.is_(False),
        User.phone_verified_at.is_not(None),
    )


def _owned_listing(actor: User, listing_id: int) -> Listing:
    _require_farmer(actor)
    listing = db.session.scalar(
        select(Listing).where(Listing.id == listing_id, Listing.seller_id == actor.id)
    )
    if listing is None:
        raise NotFound()
    return listing


def _validate_listing_values(data: dict, *, creating: bool) -> dict:
    raw_title = data.get("title")
    raw_description = data.get("description")
    if not isinstance(raw_title, str) or not isinstance(raw_description, str):
        raise MarketplaceValidationError("Enter a title and description.")
    title = " ".join(raw_title.split())
    description = raw_description.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not 3 <= len(title) <= 120:
        raise MarketplaceValidationError("Title must be between 3 and 120 characters.")
    if not description or len(description) > 5000:
        raise MarketplaceValidationError(
            "Description is required and must be at most 5,000 characters."
        )
    quantity_raw = data.get("quantity")
    if isinstance(quantity_raw, bool):
        raise MarketplaceValidationError("Quantity must be a whole number greater than zero.")
    quantity_text = str(quantity_raw).strip()
    if len(quantity_text) > 10 or not quantity_text.isascii() or not quantity_text.isdecimal():
        raise MarketplaceValidationError("Quantity must be a whole number greater than zero.")
    quantity = int(quantity_text)
    if quantity < 1 or quantity > 2_147_483_647:
        raise MarketplaceValidationError("Quantity must be greater than zero.")
    try:
        unit = Unit(str(data.get("unit")))
        grade = QualityGrade(str(data.get("grade", QualityGrade.UNGRADED)))
    except ValueError as exc:
        raise MarketplaceValidationError("Choose a supported unit and grade.") from exc
    try:
        category_id = int(data.get("category_id"))
    except (TypeError, ValueError) as exc:
        raise MarketplaceValidationError("Choose an active category.") from exc
    category = db.session.scalar(
        select(Category).where(Category.id == category_id, Category.active.is_(True))
    )
    if category is None:
        raise MarketplaceValidationError("Choose an active category.")
    price_minor = parse_kes_to_minor(data.get("price", ""))
    if price_minor <= 0:
        raise MarketplaceValidationError("Price must be greater than zero.")
    county = str(data.get("county", "")).strip()
    if county not in KENYAN_COUNTIES:
        raise MarketplaceValidationError("Choose a valid Kenyan county.")
    coordinates = {}
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        raw = data.get(key)
        if raw in (None, ""):
            coordinates[key] = None
            continue
        try:
            value = Decimal(str(raw))
        except InvalidOperation as exc:
            raise MarketplaceValidationError(f"Enter a valid {key}.") from exc
        if not value.is_finite() or value < low or value > high:
            raise MarketplaceValidationError(f"Enter a valid {key}.")
        coordinates[key] = float(value)
    return {
        "category": category,
        "title": title,
        "description": description,
        "quantity": quantity,
        "unit": unit.value,
        "grade": grade.value,
        "price_minor": price_minor,
        "county": county,
        "location_name": " ".join(str(data.get("location_name") or "").split()) or None,
        **coordinates,
    }


def image_upload_root() -> Path:
    configured = current_app.config.get("MARKETPLACE_UPLOAD_ROOT")
    if configured:
        return Path(configured)
    return Path(current_app.instance_path) / "uploads" / "listing_images"


def _validate_upload_count(listing: Listing | None, uploads: list) -> None:
    meaningful = [upload for upload in uploads if upload and upload.filename]
    current_count = len(listing.images) if listing else 0
    if current_count + len(meaningful) > MAX_IMAGES_PER_LISTING:
        raise MarketplaceValidationError("A listing can have at most six images.")


def _process_uploads(uploads: list) -> list[ProcessedImage]:
    processed: list[ProcessedImage] = []
    try:
        for upload in uploads:
            if upload and upload.filename:
                processed.append(process_image(upload, image_upload_root()))
    except (ImageValidationError, OSError) as exc:
        for image in processed:
            remove_processed_files(image)
        if isinstance(exc, ImageValidationError):
            raise MarketplaceValidationError(str(exc)) from exc
        raise MarketplaceValidationError("The image could not be saved.") from exc
    return processed


def _attach_images(listing: Listing, processed: list[ProcessedImage]) -> None:
    has_primary = any(image.is_primary for image in listing.images)
    for image in processed:
        primary = not has_primary
        listing.images.append(
            ListingImage(
                filename=image.filename,
                original_filename=image.original_filename,
                mime_type="image/jpeg",
                width=image.width,
                height=image.height,
                is_primary=primary,
            )
        )
        has_primary = has_primary or primary


def create_listing(actor: User, data: dict, uploads: list | None = None) -> Listing:
    _require_farmer(actor)
    values = _validate_listing_values(data, creating=True)
    uploads = uploads or []
    _validate_upload_count(None, uploads)
    processed = _process_uploads(uploads)
    listing = Listing(
        seller_id=actor.id,
        category_id=values.pop("category").id,
        status=ListingStatus.AVAILABLE.value,
        moderation_status=ModerationStatus.APPROVED.value,
        **values,
    )
    try:
        _attach_images(listing, processed)
        db.session.add(listing)
        db.session.commit()
        return listing
    except Exception:
        db.session.rollback()
        for image in processed:
            remove_processed_files(image)
        raise


def update_listing(
    actor: User, listing_id: int, data: dict, uploads: list | None = None
) -> Listing:
    listing = _owned_listing(actor, listing_id)
    if listing.moderation_status == ModerationStatus.REMOVED.value:
        raise MarketplaceValidationError("A removed listing cannot be edited.")
    values = _validate_listing_values(data, creating=False)
    uploads = uploads or []
    _validate_upload_count(listing, uploads)
    processed = _process_uploads(uploads)
    listing.category_id = values.pop("category").id
    for key, value in values.items():
        setattr(listing, key, value)
    try:
        _attach_images(listing, processed)
        db.session.commit()
        return listing
    except Exception:
        db.session.rollback()
        for image in processed:
            remove_processed_files(image)
        raise


_ALLOWED_STATUS_TRANSITIONS = {
    ListingStatus.AVAILABLE.value: {ListingStatus.UNAVAILABLE.value, ListingStatus.SOLD.value},
    ListingStatus.UNAVAILABLE.value: {ListingStatus.AVAILABLE.value, ListingStatus.SOLD.value},
    ListingStatus.SOLD.value: {ListingStatus.AVAILABLE.value, ListingStatus.UNAVAILABLE.value},
}


def change_listing_status(actor: User, listing_id: int, new_status: str) -> Listing:
    listing = _owned_listing(actor, listing_id)
    if listing.moderation_status == ModerationStatus.REMOVED.value:
        raise MarketplaceValidationError("A removed listing cannot change status.")
    try:
        target = ListingStatus(new_status).value
    except ValueError as exc:
        raise MarketplaceValidationError("Choose a valid listing status.") from exc
    if target != listing.status and target not in _ALLOWED_STATUS_TRANSITIONS[listing.status]:
        raise MarketplaceValidationError("That status change is not allowed.")
    listing.status = target
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return listing


def add_listing_images(actor: User, listing_id: int, uploads: list) -> Listing:
    listing = _owned_listing(actor, listing_id)
    if listing.moderation_status == ModerationStatus.REMOVED.value:
        raise MarketplaceValidationError("Images cannot be added to a removed listing.")
    _validate_upload_count(listing, uploads)
    processed = _process_uploads(uploads)
    try:
        _attach_images(listing, processed)
        db.session.commit()
    except Exception:
        db.session.rollback()
        for image in processed:
            remove_processed_files(image)
        raise
    return listing


def _owned_image(actor: User, listing_id: int, image_id: int) -> tuple[Listing, ListingImage]:
    listing = _owned_listing(actor, listing_id)
    image = db.session.scalar(
        select(ListingImage).where(
            ListingImage.id == image_id,
            ListingImage.listing_id == listing.id,
        )
    )
    if image is None:
        raise NotFound()
    return listing, image


def set_primary_image(actor: User, listing_id: int, image_id: int) -> None:
    listing, image = _owned_image(actor, listing_id, image_id)
    if listing.moderation_status == ModerationStatus.REMOVED.value:
        raise MarketplaceValidationError("Images on removed listings cannot be changed.")
    try:
        db.session.execute(
            update(ListingImage)
            .where(ListingImage.listing_id == listing.id)
            .values(is_primary=False)
        )
        image.is_primary = True
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def delete_listing_image(actor: User, listing_id: int, image_id: int) -> None:
    listing, image = _owned_image(actor, listing_id, image_id)
    if listing.moderation_status == ModerationStatus.REMOVED.value:
        raise MarketplaceValidationError("Images on removed listings cannot be changed.")
    old_filename = image.filename
    was_primary = image.is_primary
    db.session.delete(image)
    if was_primary:
        replacement = next(
            (candidate for candidate in listing.images if candidate.id != image.id), None
        )
        if replacement:
            replacement.is_primary = False
            db.session.flush()
            replacement.is_primary = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    remove_stored_files(image_upload_root(), old_filename)


def get_public_listing(listing_id: int) -> Listing:
    stmt = (
        select(Listing)
        .where(
            Listing.id == listing_id,
            Listing.status == ListingStatus.AVAILABLE.value,
            Listing.moderation_status == ModerationStatus.APPROVED.value,
            Listing.quantity > 0,
            Listing.category.has(Category.active.is_(True)),
            Listing.seller.has(_public_seller_criteria()),
        )
        .options(
            joinedload(Listing.category),
            joinedload(Listing.seller).joinedload(User.profile),
            selectinload(Listing.images),
        )
    )
    listing = db.session.scalar(stmt)
    if listing is None:
        raise NotFound()
    return listing


def get_public_seller(user_id: int) -> User:
    seller = db.session.scalar(
        select(User)
        .where(
            User.id == user_id,
            User.role == "FARMER",
            User.is_active.is_(True),
            User.is_suspended.is_(False),
            User.phone_verified_at.is_not(None),
        )
        .options(joinedload(User.profile))
    )
    if seller is None:
        raise NotFound()
    return seller


def seller_public_listings(seller_id: int):
    return public_listing_query().where(Listing.seller_id == seller_id)


def public_listing_query():
    return (
        select(Listing)
        .where(
            Listing.moderation_status == ModerationStatus.APPROVED.value,
            Listing.status == ListingStatus.AVAILABLE.value,
            Listing.quantity > 0,
            Listing.category.has(Category.active.is_(True)),
            Listing.seller.has(_public_seller_criteria()),
        )
        .options(
            joinedload(Listing.category),
            joinedload(Listing.seller).joinedload(User.profile),
            selectinload(Listing.images),
        )
    )


def _parse_filter_price(raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    value = parse_kes_to_minor(raw)
    if value < 0:
        raise MarketplaceValidationError("Price filters cannot be negative.")
    return value


def normalize_search_criteria(params) -> SearchCriteria:
    query = " ".join((params.get("q") or "").split())[:120]
    category = params.get("category") or ""
    county = params.get("county") or ""
    grade = params.get("grade") or ""
    unit = params.get("unit") or ""
    sort = params.get("sort") or "newest"
    allowed_sorts = {"newest", "oldest", "price_low", "price_high"}
    if category and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", category):
        raise MarketplaceValidationError("Choose a valid category.")
    if county and county not in KENYAN_COUNTIES:
        raise MarketplaceValidationError("Choose a valid Kenyan county.")
    if grade and grade not in {v.value for v in QualityGrade}:
        raise MarketplaceValidationError("Choose a valid grade.")
    if unit and unit not in {v.value for v in Unit}:
        raise MarketplaceValidationError("Choose a valid unit.")
    if sort not in allowed_sorts:
        sort = "newest"
    min_price = _parse_filter_price(params.get("min_price"))
    max_price = _parse_filter_price(params.get("max_price"))
    if min_price is not None and max_price is not None and min_price > max_price:
        raise MarketplaceValidationError("Minimum price must not exceed maximum price.")
    try:
        page = max(1, min(int(params.get("page", 1)), 2_147_483_647))
    except ValueError, TypeError:
        page = 1
    try:
        per_page = max(1, min(int(params.get("per_page", PAGE_SIZE)), MAX_PAGE_SIZE))
    except ValueError, TypeError:
        per_page = PAGE_SIZE
    return SearchCriteria(
        query, category, county, grade, unit, min_price, max_price, sort, page, per_page
    )


def search_listings(criteria: SearchCriteria):
    stmt = public_listing_query()
    if criteria.query:
        needle = criteria.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle}%"
        stmt = stmt.where(
            or_(
                Listing.title.ilike(pattern, escape="\\"),
                Listing.description.ilike(pattern, escape="\\"),
            )
        )
    if criteria.category:
        stmt = stmt.join(Listing.category).where(
            Category.slug == criteria.category, Category.active.is_(True)
        )
    if criteria.county:
        stmt = stmt.where(Listing.county == criteria.county)
    if criteria.grade:
        stmt = stmt.where(Listing.grade == criteria.grade)
    if criteria.unit:
        stmt = stmt.where(Listing.unit == criteria.unit)
    if criteria.min_price is not None:
        stmt = stmt.where(Listing.price_minor >= criteria.min_price)
    if criteria.max_price is not None:
        stmt = stmt.where(Listing.price_minor <= criteria.max_price)
    sort_map = {
        "newest": (Listing.created_at.desc(), Listing.id.desc()),
        "oldest": (Listing.created_at.asc(), Listing.id.asc()),
        "price_low": (Listing.price_minor.asc(), Listing.id.asc()),
        "price_high": (Listing.price_minor.desc(), Listing.id.desc()),
    }
    stmt = stmt.order_by(*sort_map.get(criteria.sort, sort_map["newest"]))
    return db.paginate(stmt, page=criteria.page, per_page=criteria.per_page, error_out=False)


def seller_management_listings(actor: User):
    _require_farmer(actor)
    return db.session.scalars(
        select(Listing)
        .where(Listing.seller_id == actor.id)
        .options(joinedload(Listing.category), selectinload(Listing.images))
        .order_by(Listing.created_at.desc(), Listing.id.desc())
    ).all()


def get_owned_listing(actor: User, listing_id: int) -> Listing:
    listing = _owned_listing(actor, listing_id)
    return db.session.scalar(
        select(Listing)
        .where(Listing.id == listing.id)
        .options(joinedload(Listing.category), selectinload(Listing.images))
    )


def get_public_listing_image(listing_id: int, image_id: int) -> ListingImage:
    listing = get_public_listing(listing_id)
    image = db.session.scalar(
        select(ListingImage).where(
            ListingImage.id == image_id, ListingImage.listing_id == listing.id
        )
    )
    if image is None:
        raise NotFound()
    return image


def get_listing_image_for_view(listing_id: int, image_id: int, actor=None) -> ListingImage:
    """Allow public images only for public listings, or their verified owner."""
    try:
        return get_public_listing_image(listing_id, image_id)
    except NotFound:
        if actor is None or not getattr(actor, "is_authenticated", False):
            raise
        if getattr(actor, "role", None) == "ADMIN":
            from app.admin_ops.services import require_admin

            require_admin(actor)
            image = db.session.scalar(
                select(ListingImage).where(
                    ListingImage.id == image_id, ListingImage.listing_id == listing_id
                )
            )
            if image is None:
                raise NotFound() from None
            return image
        listing = _owned_listing(actor, listing_id)
        image = db.session.scalar(
            select(ListingImage).where(
                ListingImage.id == image_id,
                ListingImage.listing_id == listing.id,
            )
        )
        if image is None:
            raise NotFound() from None
        return image
