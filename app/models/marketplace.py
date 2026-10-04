"""Marketplace persistence models and controlled enum values."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import User, utcnow


class ListingStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    SOLD = "SOLD"


class ModerationStatus(StrEnum):
    APPROVED = "APPROVED"
    FLAGGED = "FLAGGED"
    REMOVED = "REMOVED"


class QualityGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    UNGRADED = "UNGRADED"


class Unit(StrEnum):
    KG = "KG"
    BAG = "BAG"
    TONNE = "TONNE"
    CRATE = "CRATE"
    PIECE = "PIECE"
    LITRE = "LITRE"


class Category(db.Model):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 2 AND 80", name="ck_categories_name_length"),
        CheckConstraint("length(slug) BETWEEN 2 AND 90", name="ck_categories_slug_length"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(90), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    listings: Mapped[list[Listing]] = relationship(back_populates="category")


class Listing(db.Model):
    __tablename__ = "listings"
    __table_args__ = (
        CheckConstraint(
            "length(title) BETWEEN 3 AND 120", name="ck_listings_title_length"
        ),
        CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 5000",
            name="ck_listings_description_length",
        ),
        CheckConstraint("quantity >= 0", name="ck_listings_quantity_nonnegative"),
        CheckConstraint(
            "status != 'AVAILABLE' OR quantity > 0",
            name="ck_listings_available_quantity_positive",
        ),
        CheckConstraint("price_minor > 0", name="ck_listings_price_positive"),
        CheckConstraint(
            "unit IN ('KG', 'BAG', 'TONNE', 'CRATE', 'PIECE', 'LITRE')", name="ck_listings_unit"
        ),
        CheckConstraint("grade IN ('A', 'B', 'C', 'UNGRADED')", name="ck_listings_grade"),
        CheckConstraint(
            "status IN ('AVAILABLE', 'UNAVAILABLE', 'SOLD')", name="ck_listings_status"
        ),
        CheckConstraint(
            "moderation_status IN ('APPROVED', 'FLAGGED', 'REMOVED')",
            name="ck_listings_moderation_status",
        ),
        CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN -90 AND 90", name="ck_listings_latitude"
        ),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180",
            name="ck_listings_longitude",
        ),
        Index("ix_listings_public_created", "moderation_status", "status", "created_at"),
        Index("ix_listings_public_category", "moderation_status", "status", "category_id"),
        Index("ix_listings_public_county", "moderation_status", "status", "county"),
        Index("ix_listings_public_price", "moderation_status", "status", "price_minor"),
        Index("ix_listings_seller_created", "seller_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    grade: Mapped[str] = mapped_column(String(12), default=QualityGrade.UNGRADED, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ListingStatus.AVAILABLE, nullable=False)
    moderation_status: Mapped[str] = mapped_column(
        String(16), default=ModerationStatus.APPROVED, nullable=False
    )
    county: Mapped[str] = mapped_column(String(40), nullable=False)
    location_name: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    seller: Mapped[User] = relationship(back_populates="listings")
    category: Mapped[Category] = relationship(back_populates="listings")
    images: Mapped[list[ListingImage]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="ListingImage.id"
    )


class ListingImage(db.Model):
    __tablename__ = "listing_images"
    __table_args__ = (
        Index(
            "uq_listing_images_one_primary",
            "listing_id",
            unique=True,
            sqlite_where=text("is_primary = 1"),
            postgresql_where=text("is_primary = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(160))
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="images")
