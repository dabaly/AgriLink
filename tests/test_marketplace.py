"""Marketplace ownership, visibility, search, money, and upload tests."""

import re
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import NotFound

from app.extensions import db
from app.marketplace.constants import INITIAL_CATEGORIES
from app.marketplace.images import ImageValidationError, process_image, safe_image_path
from app.marketplace.services import (
    MarketplaceValidationError,
    add_listing_images,
    change_listing_status,
    create_listing,
    delete_listing_image,
    format_kes,
    normalize_search_criteria,
    parse_kes_to_minor,
    search_listings,
    seed_categories,
    set_primary_image,
    update_listing,
)
from app.models import Category, Listing, ListingImage, Profile, User
from app.models.marketplace import ModerationStatus


def make_user(phone, role="FARMER", *, verified=True, email=None):
    user = User(phone=phone, role=role, email=email, profile=Profile())
    user.set_password("a-long-safe-passphrase")
    if verified:
        user.phone_verified_at = datetime.now(UTC)
    db.session.add(user)
    db.session.commit()
    return user


def make_listing(user, **overrides):
    category = db.session.scalar(db.select(Category).where(Category.slug == "crops"))
    values = {
        "category_id": category.id,
        "title": "Fresh maize harvest",
        "description": "Harvested this week in Nakuru.",
        "quantity": "40",
        "unit": "BAG",
        "grade": "A",
        "price": "150.00",
        "county": "Nakuru",
        "location_name": "Njoro market",
        "latitude": "-0.3",
        "longitude": "36.1",
    }
    values.update(overrides)
    return create_listing(user, values)


def csrf(response):
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data)
    assert match, response.data.decode()
    return match.group(1).decode()


@pytest.fixture
def seeded(app):
    assert seed_categories() == len(INITIAL_CATEGORIES)
    yield


@pytest.fixture
def farmer(seeded):
    return make_user("+254711111111")


def test_seed_categories_is_idempotent_and_unique(seeded):
    assert seed_categories() == 0
    assert db.session.scalar(db.select(db.func.count(Category.id))) == 4
    db.session.add(Category(name="Crops duplicate", slug="crops"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_create_listing_normalizes_text_and_stores_integer_kes(farmer):
    listing = make_listing(farmer, title="  Fresh   maize\n harvest ", price="150.05")
    assert listing.title == "Fresh maize harvest"
    assert listing.price_minor == 15005
    assert isinstance(listing.quantity, int) and listing.quantity == 40
    assert listing.status == "AVAILABLE"
    assert listing.moderation_status == "APPROVED"
    assert format_kes(listing.price_minor) == "KES 150.05"


def test_database_rejects_available_listing_with_zero_quantity(farmer):
    listing = make_listing(farmer)
    listing.quantity = 0
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Listing, listing.id).quantity == 40


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "1.5"},
        {"quantity": "NaN"},
        {"unit": "BUSHEL"},
        {"grade": "PREMIUM"},
        {"price": "0"},
        {"price": "-10"},
        {"price": "2.999"},
        {"price": "NaN"},
        {"county": "Elsewhere"},
        {"category_id": "9999"},
        {"latitude": "NaN"},
        {"description": None},
        {"title": None},
    ],
)
def test_invalid_listing_values_are_rejected(farmer, changes):
    with pytest.raises(MarketplaceValidationError):
        make_listing(farmer, **changes)


def test_buyer_and_unverified_farmer_cannot_create(seeded):
    buyer = make_user("+254722222222", "BUYER")
    unverified = make_user("+254733333333", verified=False)
    with pytest.raises(NotFound):
        make_listing(buyer)
    with pytest.raises(NotFound):
        make_listing(unverified)


def test_service_uses_actor_identity_and_scopes_edits(farmer):
    listing = make_listing(farmer)
    other = make_user("+254744444444")
    buyer = make_user("+254755555555", "BUYER")
    with pytest.raises(NotFound):
        update_listing(other, listing.id, {})
    with pytest.raises(NotFound):
        update_listing(buyer, listing.id, {})
    updated = update_listing(
        farmer,
        listing.id,
        {
            "category_id": listing.category_id,
            "title": "Fresh maize from farm",
            "description": "Clean maize, ready for collection.",
            "quantity": "20",
            "unit": "BAG",
            "grade": "UNGRADED",
            "price": "175",
            "county": "Nakuru",
            "location_name": "Njoro",
            "latitude": "",
            "longitude": "",
            "seller_id": other.id,
        },
    )
    assert updated.seller_id == farmer.id
    assert updated.quantity == 20
    assert updated.price_minor == 17500


def test_status_management_and_public_visibility(farmer, client):
    available = make_listing(farmer)
    unavailable = make_listing(farmer, title="Unavailable beans")
    sold = make_listing(farmer, title="Sold potatoes")
    removed = make_listing(farmer, title="Removed onions")
    change_listing_status(farmer, unavailable.id, "UNAVAILABLE")
    change_listing_status(farmer, sold.id, "SOLD")
    removed.moderation_status = ModerationStatus.REMOVED.value
    db.session.commit()
    page = client.get("/listings")
    assert page.status_code == 200
    assert b"Fresh maize harvest" in page.data
    assert b"Unavailable beans" not in page.data
    assert b"Sold potatoes" not in page.data
    assert b"Removed onions" not in page.data
    assert client.get(f"/listings/{unavailable.id}").status_code == 404
    assert client.get(f"/listings/{removed.id}").status_code == 404
    assert client.get(f"/sellers/{farmer.id}").status_code == 200
    assert b"Unavailable beans" not in client.get(f"/sellers/{farmer.id}").data
    assert available.id != unavailable.id


def test_search_escaping_filters_sort_and_pagination(farmer):
    one = make_listing(farmer, title="Red beans", description="Small red beans", price="100")
    two = make_listing(farmer, title="Green maize", description="Organic maize seed", price="200")
    three = make_listing(farmer, title="Yellow beans", description="Premium beans", price="300")
    for listing, created in ((one, 3), (two, 2), (three, 1)):
        listing.created_at = datetime(2026, 1, created, tzinfo=UTC)
    db.session.commit()
    organic = search_listings(normalize_search_criteria({"q": "ORGANIC"}))
    assert [r.title for r in organic.items] == ["Green maize"]
    assert search_listings(normalize_search_criteria({"q": "%"})).total == 0
    low = search_listings(normalize_search_criteria({"min_price": "150", "max_price": "250"}))
    assert [r.id for r in low.items] == [two.id]
    assert search_listings(normalize_search_criteria({"sort": "price_low"})).items[0].id == one.id
    assert search_listings(normalize_search_criteria({"sort": "oldest"})).items[0].id == three.id
    combined = normalize_search_criteria(
        {
            "category": "crops",
            "county": "Nakuru",
            "grade": "A",
            "unit": "BAG",
            "min_price": "100",
            "max_price": "200",
        }
    )
    assert search_listings(combined).total == 2
    assert search_listings(normalize_search_criteria({"sort": "untrusted"})).total == 3
    invalid_page = normalize_search_criteria({"page": "invalid", "per_page": "500"})
    assert search_listings(invalid_page).page == 1
    assert search_listings(normalize_search_criteria({"q": "' OR 1=1 --"})).total == 0
    page = search_listings(normalize_search_criteria({"page": "2", "per_page": "999"}))
    assert page.page == 2 and page.per_page == 24
    assert page.total == 3


def test_money_rejects_float_syntax_and_parses_minor_units():
    assert parse_kes_to_minor("0.01") == 1
    assert parse_kes_to_minor("125") == 12500
    for value in ("1.001", "NaN", "Infinity", "-1", "1e4", "1,000"):
        with pytest.raises(MarketplaceValidationError):
            parse_kes_to_minor(value)
    with pytest.raises(MarketplaceValidationError):
        parse_kes_to_minor(150.0)


def test_image_pipeline_reencodes_resizes_and_strips_exif(app):
    from werkzeug.datastructures import FileStorage

    image = Image.new("RGB", (2200, 1000), color="green")
    exif = Image.Exif()
    exif[270] = "GPS private note"
    source = BytesIO()
    image.save(source, format="JPEG", exif=exif)
    source.seek(0)
    upload = FileStorage(stream=source, filename="../../my-photo.jpg", content_type="image/jpeg")
    with app.app_context():
        processed = process_image(upload, __import__("pathlib").Path(app.instance_path) / "uploads")
        assert processed.filename.endswith(".jpg")
        assert processed.filename != "../../my-photo.jpg"
        assert processed.width == 1600 and processed.height == 727
        with Image.open(processed.path) as normalized:
            assert normalized.getexif().get(270) is None
            assert normalized.format == "JPEG"
        assert safe_image_path(processed.path.parent, "../../.env") is None
        processed.path.unlink()
        processed.thumbnail_path.unlink()


def test_non_image_and_image_over_limit_rejected(app):
    from werkzeug.datastructures import FileStorage

    with app.app_context():
        with pytest.raises(ImageValidationError):
            process_image(
                FileStorage(stream=BytesIO(b"not an image"), filename="wrong.jpg"),
                __import__("pathlib").Path(app.instance_path) / "uploads",
            )
        huge = Image.new("RGB", (100, 100), color="red")
        source = BytesIO()
        huge.save(source, format="PNG")
        source.seek(0)
        # A misleading extension does not change the content Pillow detects.
        result = process_image(
            FileStorage(stream=source, filename="not-a-jpeg.txt"),
            __import__("pathlib").Path(app.instance_path) / "uploads",
        )
        result.path.unlink()
        result.thumbnail_path.unlink()


def test_image_byte_and_pixel_limits_rejected(app, monkeypatch):
    from pathlib import Path

    from werkzeug.datastructures import FileStorage

    import app.marketplace.images as image_module

    with app.app_context():
        too_large = FileStorage(
            stream=BytesIO(b"x" * (image_module.MAX_IMAGE_BYTES + 1)), filename="big.jpg"
        )
        with pytest.raises(ImageValidationError, match="5 MB"):
            process_image(too_large, Path(app.instance_path) / "uploads")
        monkeypatch.setattr(image_module, "MAX_IMAGE_PIXELS", 100)
        source = BytesIO()
        Image.new("RGB", (11, 10)).save(source, format="PNG")
        source.seek(0)
        with pytest.raises(ImageValidationError, match="dimensions"):
            process_image(
                FileStorage(stream=source, filename="pixels.png"),
                Path(app.instance_path) / "uploads",
            )


def test_image_ownership_primary_uniqueness_and_six_image_limit(farmer):
    from werkzeug.datastructures import FileStorage

    other = make_user("+254766666666")
    source = BytesIO()
    Image.new("RGB", (80, 60), color="blue").save(source, format="PNG")
    first = FileStorage(stream=BytesIO(source.getvalue()), filename="first.png")
    second = FileStorage(stream=BytesIO(source.getvalue()), filename="second.png")
    listing = make_listing(farmer)
    add_listing_images(farmer, listing.id, [first, second])
    images = db.session.scalars(
        db.select(ListingImage)
        .where(ListingImage.listing_id == listing.id)
        .order_by(ListingImage.id)
    ).all()
    assert len(images) == 2
    assert sum(image.is_primary for image in images) == 1
    with pytest.raises(NotFound):
        set_primary_image(other, listing.id, images[1].id)
    set_primary_image(farmer, listing.id, images[1].id)
    db.session.expire_all()
    images = db.session.scalars(
        db.select(ListingImage).where(ListingImage.listing_id == listing.id)
    ).all()
    assert sum(image.is_primary for image in images) == 1
    with pytest.raises(NotFound):
        delete_listing_image(other, listing.id, images[0].id)
    too_many = [FileStorage(stream=BytesIO(b"x"), filename=f"{i}.jpg") for i in range(7)]
    with pytest.raises(MarketplaceValidationError, match="six images"):
        add_listing_images(farmer, listing.id, too_many)
    primary = next(image for image in images if image.is_primary)
    delete_listing_image(farmer, listing.id, primary.id)
    remaining = db.session.scalar(
        db.select(ListingImage).where(ListingImage.listing_id == listing.id)
    )
    assert remaining.is_primary


def test_public_api_does_not_expose_private_seller_data(farmer, client):
    farmer.email = "private@example.test"
    db.session.commit()
    make_listing(farmer)
    response = client.get("/api/listings")
    assert response.status_code == 200
    payload = response.get_json()["items"][0]
    serialized = str(payload).lower()
    assert "phone" not in serialized
    assert "email" not in serialized
    assert "private@example.test" not in serialized
    assert payload["price_minor"] == 15000
    assert payload["latitude"] == -0.3


def test_csrf_required_for_status_change(client, farmer, app):
    listing = make_listing(farmer)
    login_page = client.get("/auth/login")
    client.post(
        "/auth/login",
        data={
            "csrf_token": csrf(login_page),
            "phone": farmer.phone,
            "password": "a-long-safe-passphrase",
        },
    )
    page = client.get("/my/listings")
    assert page.status_code == 200
    denied = client.post(f"/listings/{listing.id}/status", data={"status": "SOLD"})
    assert denied.status_code == 400
    response = client.post(
        f"/listings/{listing.id}/status",
        data={"csrf_token": csrf(page), "status": "SOLD"},
    )
    assert response.status_code == 302
    assert db.session.get(Listing, listing.id).status == "SOLD"


def test_marketplace_end_to_end_farmer_workflow(client, seeded):
    farmer = make_user("+254777777777", email="farmer@example.test")
    login_page = client.get("/auth/login")
    client.post(
        "/auth/login",
        data={
            "csrf_token": csrf(login_page),
            "phone": farmer.phone,
            "password": "a-long-safe-passphrase",
        },
    )
    category = db.session.scalar(db.select(Category).where(Category.slug == "crops"))
    listing_page = client.get("/listings/new")
    image = BytesIO()
    Image.new("RGB", (120, 90), color="orange").save(image, format="PNG")
    created = client.post(
        "/listings/new",
        data={
            "csrf_token": csrf(listing_page),
            "category_id": str(category.id),
            "title": "Fresh oranges from farm",
            "description": "Sweet oranges harvested this week.",
            "quantity": "500",
            "unit": "KG",
            "grade": "A",
            "price": "85.50",
            "county": "Makueni",
            "location_name": "Wote market",
            "latitude": "-1.78",
            "longitude": "37.63",
            "seller_id": "987654",
            "images": (BytesIO(image.getvalue()), "orange.png"),
        },
        content_type="multipart/form-data",
    )
    assert created.status_code == 302
    listing = db.session.scalar(
        db.select(Listing).where(Listing.title == "Fresh oranges from farm")
    )
    assert listing.seller_id == farmer.id and listing.price_minor == 8550
    assert len(listing.images) == 1 and listing.images[0].is_primary
    assert client.get(created.location).status_code == 200
    image_response = client.get(f"/listing-images/{listing.id}/{listing.images[0].id}/full")
    assert image_response.status_code == 200
    assert image_response.mimetype == "image/jpeg"
    assert client.get("/listings?q=oranges&county=Makueni").status_code == 200
    assert client.get("/api/listings?category=crops").get_json()["total"] == 1
    assert client.get(f"/sellers/{farmer.id}").status_code == 200

    edit_page = client.get(f"/listings/{listing.id}/edit")
    edited = client.post(
        f"/listings/{listing.id}/edit",
        data={
            "csrf_token": csrf(edit_page),
            "category_id": str(category.id),
            "title": "Fresh sweet oranges",
            "description": "Sweet oranges harvested this week in Makueni.",
            "quantity": "450",
            "unit": "KG",
            "grade": "A",
            "price": "90.00",
            "county": "Makueni",
            "location_name": "Wote market",
            "latitude": "-1.78",
            "longitude": "37.63",
        },
    )
    assert edited.status_code == 302
    assert db.session.get(Listing, listing.id).title == "Fresh sweet oranges"

    csrf_value = csrf(client.get("/my/listings"))
    client.post(
        f"/listings/{listing.id}/status",
        data={"csrf_token": csrf_value, "status": "UNAVAILABLE"},
    )
    assert client.get("/listings").get_data(as_text=True).find("Fresh sweet oranges") == -1

    logout_page = client.get("/my/listings")
    client.post("/auth/logout", data={"csrf_token": csrf(logout_page)})
    assert client.get(f"/listings/{listing.id}").status_code == 404
    assert client.get(
        f"/listing-images/{listing.id}/{listing.images[0].id}/full"
    ).status_code == 404
    other = make_user("+254788888888")
    login_page = client.get("/auth/login")
    client.post(
        "/auth/login",
        data={
            "csrf_token": csrf(login_page),
            "phone": other.phone,
            "password": "a-long-safe-passphrase",
        },
    )
    assert client.get(f"/listings/{listing.id}/edit").status_code == 404
    assert (
        client.post(
            f"/listings/{listing.id}/status",
            data={"csrf_token": csrf(client.get("/my/listings")), "status": "SOLD"},
        ).status_code
        == 404
    )
