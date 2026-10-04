"""Public marketplace browsing and farmer listing management routes."""

from __future__ import annotations

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from sqlalchemy import select

from app.auth.decorators import role_required
from app.extensions import db
from app.marketplace import marketplace_bp
from app.marketplace.constants import KENYAN_COUNTIES
from app.marketplace.forms import ListingForm, ListingSearchForm
from app.marketplace.images import safe_image_path
from app.marketplace.services import (
    MarketplaceValidationError,
    add_listing_images,
    change_listing_status,
    create_listing,
    delete_listing_image,
    format_kes,
    get_listing_image_for_view,
    get_owned_listing,
    get_public_listing,
    get_public_seller,
    image_upload_root,
    normalize_search_criteria,
    search_listings,
    seller_management_listings,
    seller_public_listings,
    set_primary_image,
    update_listing,
)
from app.models import Category, Listing
from app.models.marketplace import ListingStatus, Unit
from app.reviews.services import ReviewService


def _active_categories():
    return db.session.scalars(
        select(Category).where(Category.active.is_(True)).order_by(Category.name)
    ).all()


def _listing_data(form):
    return {
        "category_id": form.category_id.data,
        "title": form.title.data,
        "description": form.description.data,
        "quantity": form.quantity.data,
        "unit": form.unit.data,
        "grade": form.grade.data,
        "price": form.price.data,
        "county": form.county.data,
        "location_name": form.location_name.data,
        "latitude": form.latitude.data,
        "longitude": form.longitude.data,
    }


def _form_template(form, *, heading, listing=None):
    return render_template(
        "marketplace/listing_form.html",
        form=form,
        heading=heading,
        listing=listing,
        statuses=ListingStatus,
    )


@marketplace_bp.get("/listings")
def browse():
    categories = _active_categories()
    form = ListingSearchForm(request.args, meta={"csrf": False})
    form.category.choices = [("", "All categories"), *((c.slug, c.name) for c in categories)]
    form.county.choices = [("", "All counties"), *((c, c) for c in KENYAN_COUNTIES)]
    form.grade.choices = [("", "Any grade"), *((g, g.title()) for g in ("A", "B", "C", "UNGRADED"))]
    form.unit.choices = [("", "Any unit"), *((u.value, u.value.title()) for u in Unit)]
    try:
        criteria = normalize_search_criteria(request.args)
        pagination = search_listings(criteria)
    except MarketplaceValidationError as exc:
        flash(str(exc), "warning")
        criteria = normalize_search_criteria({})
        pagination = search_listings(criteria)
    visible_pages = list(
        pagination.iter_pages(left_edge=1, right_edge=1, left_current=1, right_current=2)
    )
    return render_template(
        "marketplace/browse.html",
        form=form,
        pagination=pagination,
        criteria=criteria,
        visible_pages=visible_pages,
        page_urls={
            number: url_for(
                "marketplace.browse",
                **{**request.args.to_dict(flat=True), "page": number},
            )
            for number in visible_pages
            if number is not None
        },
    )


@marketplace_bp.get("/listings/<int:listing_id>")
def detail(listing_id):
    listing = get_public_listing(listing_id)
    return render_template(
        "marketplace/detail.html",
        listing=listing,
        reviews=ReviewService.public_reviews_for_listing(listing.id),
        rating_average=ReviewService.seller_review_summary(listing.seller_id)[0],
        rating_count=ReviewService.seller_review_summary(listing.seller_id)[1],
    )


@marketplace_bp.get("/sellers/<int:user_id>")
def seller_profile(user_id):
    seller = get_public_seller(user_id)
    listings = db.paginate(
        seller_public_listings(seller.id).order_by(Listing.created_at.desc(), Listing.id.desc()),
        page=min(max(1, request.args.get("page", 1, type=int)), 2_147_483_647),
        per_page=12,
        error_out=False,
    )
    rating_average, rating_count = ReviewService.seller_review_summary(seller.id)
    return render_template(
        "marketplace/seller.html",
        seller=seller,
        pagination=listings,
        rating_average=rating_average,
        rating_count=rating_count,
        reviews=ReviewService.public_reviews_for_seller(seller.id),
    )


@marketplace_bp.get("/my/listings")
@role_required("FARMER")
def my_listings():
    listings = seller_management_listings(current_user)
    return render_template("marketplace/mine.html", listings=listings)


@marketplace_bp.route("/listings/new", methods=["GET", "POST"])
@role_required("FARMER")
def new_listing():
    categories = _active_categories()
    form = ListingForm(categories=categories)
    if request.method == "GET":
        form.grade.data = "UNGRADED"
    if form.validate_on_submit():
        try:
            listing = create_listing(current_user, _listing_data(form), form.images.data)
        except MarketplaceValidationError as exc:
            flash(str(exc), "danger")
        else:
            flash("Your listing is now on the marketplace.", "success")
            return redirect(url_for("marketplace.detail", listing_id=listing.id))
    return _form_template(form, heading="List your produce")


@marketplace_bp.route("/listings/<int:listing_id>/edit", methods=["GET", "POST"])
@role_required("FARMER")
def edit_listing(listing_id):
    listing = get_owned_listing(current_user, listing_id)
    categories = _active_categories()
    form = ListingForm(categories=categories)
    if request.method == "GET":
        form.category_id.data = listing.category_id
        form.title.data = listing.title
        form.description.data = listing.description
        form.quantity.data = str(listing.quantity)
        form.unit.data = listing.unit
        form.grade.data = listing.grade
        form.price.data = f"{listing.price_minor // 100}.{listing.price_minor % 100:02d}"
        form.latitude.data = "" if listing.latitude is None else str(listing.latitude)
        form.longitude.data = "" if listing.longitude is None else str(listing.longitude)
    if form.validate_on_submit():
        try:
            listing = update_listing(
                current_user, listing_id, _listing_data(form), form.images.data
            )
        except MarketplaceValidationError as exc:
            flash(str(exc), "danger")
        else:
            flash("Listing updated.", "success")
            return redirect(url_for("marketplace.my_listings"))
    return _form_template(form, heading="Edit listing", listing=listing)


@marketplace_bp.post("/listings/<int:listing_id>/status")
@role_required("FARMER")
def listing_status(listing_id):
    try:
        change_listing_status(current_user, listing_id, request.form.get("status", ""))
    except MarketplaceValidationError as exc:
        flash(str(exc), "danger")
    else:
        flash("Listing availability updated.", "success")
    return redirect(url_for("marketplace.my_listings"))


@marketplace_bp.post("/listings/<int:listing_id>/images")
@role_required("FARMER")
def upload_listing_images(listing_id):
    try:
        add_listing_images(current_user, listing_id, request.files.getlist("images"))
    except MarketplaceValidationError as exc:
        flash(str(exc), "danger")
    else:
        flash("Photos added.", "success")
    return redirect(url_for("marketplace.edit_listing", listing_id=listing_id))


@marketplace_bp.post("/listings/<int:listing_id>/images/<int:image_id>/primary")
@role_required("FARMER")
def primary_listing_image(listing_id, image_id):
    try:
        set_primary_image(current_user, listing_id, image_id)
    except MarketplaceValidationError as exc:
        flash(str(exc), "danger")
    else:
        flash("Primary photo updated.", "success")
    return redirect(url_for("marketplace.edit_listing", listing_id=listing_id))


@marketplace_bp.post("/listings/<int:listing_id>/images/<int:image_id>/delete")
@role_required("FARMER")
def remove_listing_image(listing_id, image_id):
    try:
        delete_listing_image(current_user, listing_id, image_id)
    except MarketplaceValidationError as exc:
        flash(str(exc), "danger")
    else:
        flash("Photo removed.", "success")
    return redirect(url_for("marketplace.edit_listing", listing_id=listing_id))


@marketplace_bp.get("/listing-images/<int:listing_id>/<int:image_id>/<variant>")
def listing_image(listing_id, image_id, variant):
    if variant not in {"full", "thumb"}:
        abort(404)
    image = get_listing_image_for_view(listing_id, image_id, current_user)
    path = safe_image_path(
        image_upload_root(),
        image.filename,
        thumbnail=variant == "thumb",
    )
    if path is None:
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True, max_age=3600)


@marketplace_bp.get("/api/listings")
def api_listings():
    try:
        criteria = normalize_search_criteria(request.args)
    except MarketplaceValidationError as exc:
        return jsonify({"error": str(exc)}), 422
    page = search_listings(criteria)
    data = []
    for listing in page.items:
        primary = next((image for image in listing.images if image.is_primary), None)
        data.append(
            {
                "id": listing.id,
                "title": listing.title,
                "description": listing.description,
                "category": {"name": listing.category.name, "slug": listing.category.slug},
                "quantity": listing.quantity,
                "unit": listing.unit,
                "grade": listing.grade,
                "availability": listing.status,
                "price_minor": listing.price_minor,
                "price": format_kes(listing.price_minor),
                "county": listing.county,
                "location_name": listing.location_name,
                "latitude": round(listing.latitude, 2) if listing.latitude is not None else None,
                "longitude": round(listing.longitude, 2) if listing.longitude is not None else None,
                "seller": {"id": listing.seller.id, "name": _seller_name(listing.seller)},
                "image_url": url_for(
                    "marketplace.listing_image",
                    listing_id=listing.id,
                    image_id=primary.id,
                    variant="thumb",
                    _external=False,
                )
                if primary
                else None,
                "url": url_for("marketplace.detail", listing_id=listing.id, _external=False),
            }
        )
    return jsonify(
        {
            "items": data,
            "page": page.page,
            "per_page": page.per_page,
            "pages": page.pages,
            "total": page.total,
        }
    )


def _seller_name(seller):
    profile = seller.profile
    if profile:
        for key in ("display_name", "full_name", "name"):
            value = getattr(profile, key, None)
            if value:
                return value
    return "AgriLink farmer"
