"""Reusable authentication authorization decorators."""

from functools import wraps

from flask import flash, redirect, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import NotFound


def verified_phone_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.phone_verified_at is None:
            flash("Verify your phone number to continue.", "warning")
            return redirect(url_for("auth.verify"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @verified_phone_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                return "Not found", 404
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    """Require an active, phone-verified administrator; conceal private routes."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if (
            current_user.role != "ADMIN"
            or not current_user.is_active
            or current_user.is_suspended
            or current_user.phone_verified_at is None
        ):
            raise NotFound()
        return view(*args, **kwargs)

    return wrapped
