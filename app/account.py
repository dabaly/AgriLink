"""Minimal authenticated account page."""

from flask import Blueprint, render_template
from flask_login import current_user

from app.auth.decorators import verified_phone_required

account_bp = Blueprint("account", __name__, url_prefix="/account")


@account_bp.get("/profile")
@verified_phone_required
def profile():
    return render_template("account/profile.html", user=current_user)
