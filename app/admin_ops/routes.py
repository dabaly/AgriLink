"""Admin operations pages; state changes delegate to service boundaries."""

from __future__ import annotations

import json

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.exceptions import Conflict, NotFound

from app.admin_ops import admin_bp, services
from app.admin_ops.forms import ConfirmActionForm
from app.auth.decorators import admin_required
from app.models.marketplace import ListingStatus, ModerationStatus


@admin_bp.get("")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", counts=services.dashboard_counts(current_user))


@admin_bp.get("/users")
@admin_required
def users():
    pagination = services.list_users(
        current_user,
        role=request.args.get("role", ""),
        state=request.args.get("state", ""),
        verified=request.args.get("verified", ""),
        page=request.args.get("page", 1, type=int) or 1,
    )
    return render_template("admin/users.html", pagination=pagination)


@admin_bp.get("/users/<int:user_id>")
@admin_required
def user_detail(user_id: int):
    try:
        user = services.get_user(current_user, user_id)
    except NotFound:
        abort(404)
    return render_template("admin/user_detail.html", user=user, form=ConfirmActionForm())


@admin_bp.post("/users/<int:user_id>/suspension")
@admin_required
def user_suspension(user_id: int):
    form = ConfirmActionForm()
    if not form.validate_on_submit():
        abort(400)
    suspended = request.form.get("suspended") == "1"
    try:
        services.set_user_suspended(current_user, user_id, suspended=suspended)
    except NotFound:
        abort(404)
    except Conflict as exc:
        flash(str(exc), "warning")
    else:
        flash("Account suspended." if suspended else "Account reactivated.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.get("/listings")
@admin_required
def listings():
    pagination = services.list_listings(
        current_user,
        moderation=request.args.get("moderation", ""),
        status=request.args.get("status", ""),
        category=request.args.get("category", ""),
        page=request.args.get("page", 1, type=int) or 1,
    )
    return render_template(
        "admin/listings.html",
        pagination=pagination,
        moderation_values=[value.value for value in ModerationStatus],
        status_values=[value.value for value in ListingStatus],
    )


@admin_bp.get("/listings/<int:listing_id>")
@admin_required
def listing_detail(listing_id: int):
    try:
        listing = services.get_listing(current_user, listing_id)
    except NotFound:
        abort(404)
    return render_template("admin/listing_detail.html", listing=listing, form=ConfirmActionForm())


@admin_bp.post("/listings/<int:listing_id>/moderation/<action>")
@admin_required
def moderate_listing(listing_id: int, action: str):
    form = ConfirmActionForm()
    if not form.validate_on_submit():
        abort(400)
    try:
        services.moderate_listing(current_user, listing_id, action)
    except NotFound:
        abort(404)
    except (Conflict, ValueError) as exc:
        flash(str(exc), "warning")
    else:
        flash("Listing moderation updated.", "success")
    return redirect(url_for("admin.listing_detail", listing_id=listing_id))


@admin_bp.get("/audit")
@admin_required
def audit():
    pagination = services.list_audit(
        current_user,
        action=request.args.get("action", ""),
        target_type=request.args.get("target_type", ""),
        page=request.args.get("page", 1, type=int) or 1,
    )
    audit_metadata = {
        entry.id: json.loads(entry.metadata_json or "{}") for entry in pagination.items
    }
    return render_template("admin/audit.html", pagination=pagination, audit_metadata=audit_metadata)
