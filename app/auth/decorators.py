"""Reusable authentication authorization decorators."""

from functools import wraps

from flask import flash, redirect, url_for
from flask_login import current_user, login_required


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
