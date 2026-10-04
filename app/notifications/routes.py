"""Authenticated notification center and safe internal target navigation."""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import NotFound

from app.notifications import notifications_bp
from app.notifications.services import NotificationService


@notifications_bp.get("/notifications")
@login_required
def center():
    try:
        pagination = NotificationService.list_for_user(
            current_user, page=request.args.get("page", 1, type=int) or 1, per_page=20
        )
    except NotFound:
        abort(404)
    return render_template("notifications/center.html", pagination=pagination)


@notifications_bp.post("/notifications/<int:notification_id>/read")
@login_required
def read_one(notification_id: int):
    try:
        NotificationService.mark_read(current_user, notification_id)
    except NotFound:
        abort(404)
    return redirect(url_for("notifications.center"))


@notifications_bp.post("/notifications/read-all")
@login_required
def read_all():
    try:
        changed = NotificationService.mark_all_read(current_user)
    except NotFound:
        abort(404)
    flash(f"Marked {changed} notification{'s' if changed != 1 else ''} as read.", "success")
    return redirect(url_for("notifications.center"))


@notifications_bp.get("/notifications/<int:notification_id>/open")
@login_required
def open_target(notification_id: int):
    try:
        target_type, target_id = NotificationService.target_for_navigation(
            current_user, notification_id
        )
    except NotFound:
        abort(404)
    if target_type == "conversation":
        return redirect(url_for("chat.conversation_detail", conversation_id=target_id))
    if target_type == "order":
        return redirect(url_for("orders.detail", order_id=target_id))
    if target_type == "listing":
        return redirect(url_for("marketplace.detail", listing_id=target_id))
    if target_type == "seller":
        return redirect(url_for("marketplace.seller_profile", user_id=target_id))
    flash("This notification does not have an available page.", "info")
    return redirect(url_for("notifications.center"))
