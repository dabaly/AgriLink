"""CSRF-protected review submission route."""

from __future__ import annotations

from flask import abort, flash, redirect, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import Conflict, NotFound

from app.reviews import reviews_bp
from app.reviews.forms import ReviewForm
from app.reviews.services import ReviewService, ReviewValidationError


@reviews_bp.post("/orders/<int:order_id>/reviews")
@login_required
def submit(order_id: int):
    form = ReviewForm()
    if not form.validate_on_submit():
        flash("Choose a rating and enter a plain-text review.", "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    try:
        ReviewService.create_review(current_user, order_id, form.rating.data, form.body.data)
    except NotFound:
        abort(404)
    except (ReviewValidationError, Conflict) as exc:
        flash(str(exc), "warning")
    else:
        flash("Thanks for reviewing your completed purchase.", "success")
    return redirect(url_for("orders.detail", order_id=order_id))
