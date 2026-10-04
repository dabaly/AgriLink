"""Authentication routes: presentation only; state changes live in services."""

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_limiter.util import get_remote_address
from flask_login import current_user, login_user, logout_user

from app.auth import auth_bp
from app.auth.forms import ChooseRoleForm, LoginForm, RegisterForm, ResendOtpForm, VerifyOtpForm
from app.auth.services import (
    AuthenticationError,
    authenticate,
    choose_role,
    record_otp_ip_request,
    register_user,
    resend_otp,
    verify_otp,
)
from app.extensions import limiter


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("account.profile"))
    form = RegisterForm()
    if form.validate_on_submit():
        try:
            request_ip = get_remote_address()
            record_otp_ip_request(request_ip)
            user, _challenge = register_user(
                form.phone.data,
                form.password.data,
                current_app.extensions["agri_link.otp_provider"],
                request_ip,
            )
        except AuthenticationError as exc:
            flash(str(exc), "danger")
        else:
            _clear_session_preserving_csrf()
            session["pending_user_id"] = user.id
            flash("We sent a verification code to your phone.", "success")
            return redirect(url_for("auth.verify"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/verify", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def verify():
    user_id = session.get("pending_user_id")
    if not user_id:
        return redirect(url_for("auth.register"))
    form = VerifyOtpForm()
    resend_form = ResendOtpForm()
    if form.validate_on_submit():
        try:
            record_otp_ip_request(get_remote_address())
            verify_otp(user_id, form.code.data)
        except AuthenticationError as exc:
            flash(str(exc), "danger")
        else:
            session.pop("pending_user_id", None)
            session["verified_pending_user_id"] = user_id
            flash("Phone verified. Choose how you will use AgriLink.", "success")
            return redirect(url_for("auth.choose_role_route"))
    return render_template("auth/verify.html", form=form, resend_form=resend_form)


@auth_bp.post("/resend-code")
@limiter.limit("20 per hour", methods=["POST"])
def resend_code():
    user_id = session.get("pending_user_id")
    if not user_id:
        return redirect(url_for("auth.register"))
    try:
        request_ip = get_remote_address()
        record_otp_ip_request(request_ip)
        resend_otp(user_id, current_app.extensions["agri_link.otp_provider"], request_ip)
    except AuthenticationError as exc:
        flash(str(exc), "warning")
    else:
        flash("A new verification code was sent.", "success")
    return redirect(url_for("auth.verify"))


@auth_bp.route("/choose-role", methods=["GET", "POST"])
def choose_role_route():
    user_id = session.get("verified_pending_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))
    form = ChooseRoleForm()
    if form.validate_on_submit():
        try:
            user = choose_role(user_id, form.role.data)
        except AuthenticationError as exc:
            flash(str(exc), "danger")
        else:
            _clear_session_preserving_csrf()
            login_user(user, fresh=True)
            session.permanent = True
            return redirect(url_for("account.profile"))
    return render_template("auth/choose_role.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
@limiter.limit("20 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("account.profile"))
    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = authenticate(form.phone.data, form.password.data)
        except AuthenticationError as exc:
            flash(str(exc), "danger")
        else:
            if user.phone_verified_at is None:
                _clear_session_preserving_csrf()
                session["pending_user_id"] = user.id
                try:
                    request_ip = get_remote_address()
                    record_otp_ip_request(request_ip)
                    resend_otp(
                        user.id,
                        current_app.extensions["agri_link.otp_provider"],
                        request_ip,
                    )
                except AuthenticationError as exc:
                    flash(str(exc), "warning")
                else:
                    flash("We sent a verification code to your phone.", "success")
                return redirect(url_for("auth.verify"))
            if user.role is None:
                _clear_session_preserving_csrf()
                session["verified_pending_user_id"] = user.id
                flash("Choose a role to finish setting up your account.", "info")
                return redirect(url_for("auth.choose_role_route"))
            destination = _safe_next(request.args.get("next"))
            _clear_session_preserving_csrf()
            login_user(user, fresh=True)
            session.permanent = True
            return redirect(destination or url_for("account.profile"))
    return render_template("auth/login.html", form=form)


@auth_bp.post("/logout")
def logout():
    logout_user()
    _clear_session_preserving_csrf()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


def _safe_next(value: str | None) -> str | None:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("//"):
        return None
    return value


def _clear_session_preserving_csrf() -> None:
    """Rotate auth session state without dropping Flask-WTF's CSRF session seed."""
    csrf_seed = session.get("csrf_token")
    session.clear()
    if csrf_seed:
        session["csrf_token"] = csrf_seed
