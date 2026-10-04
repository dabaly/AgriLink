"""Authentication and OTP business rules."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.integrations.otp import (
    AfricaTalkingOTPProvider,
    MockOTPProvider,
    OTPDeliveryError,
    OTPProvider,
)
from app.models import OtpChallenge, OtpRequestEvent, Profile, User
from app.models.user import utcnow
from app.utils.phone import normalize_phone

PASSWORD_MIN_LENGTH = 10
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 15 * 60
OTP_GENERIC_ERROR = "That code is invalid or expired. Request a new code and try again."
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(24))


class AuthenticationError(ValueError):
    """Expected authentication or authorization failure suitable for display."""


def load_user(user_id: str) -> User | None:
    try:
        user = db.session.get(User, int(user_id))
    except TypeError, ValueError:
        return None
    if user is None or not user.is_active or user.is_suspended:
        return None
    return user


def get_otp_provider(app=None) -> OTPProvider:
    """Build the configured provider (tests may replace app.extensions value)."""
    settings = app.config if app else current_app.config
    provider_name = settings.get("OTP_PROVIDER", "mock").lower()
    if provider_name == "mock":
        return MockOTPProvider()
    if provider_name == "africastalking":
        return AfricaTalkingOTPProvider()
    raise RuntimeError("OTP_PROVIDER must be 'mock' or 'africastalking'")


def register_user(phone: str, password: str, provider: OTPProvider, request_ip: str | None):
    normalized_phone = normalize_phone(phone)
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthenticationError("Password must be at least 10 characters.")
    if db.session.scalar(select(User.id).where(User.phone == normalized_phone)):
        raise AuthenticationError(
            "An account already uses that phone number. Log in or verify your account."
        )

    user = User(phone=normalized_phone)
    user.set_password(password)
    user.profile = Profile()
    db.session.add(user)
    try:
        db.session.flush()
        challenge = _create_challenge(user, provider, request_ip)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise AuthenticationError(
            "An account already uses that phone number. Log in or verify your account."
        ) from exc
    return user, challenge


def record_otp_ip_request(request_ip: str | None) -> None:
    """Enforce the hourly OTP request cap using persistent database state."""
    if not request_ip:
        return
    now = utcnow()
    since = now - timedelta(hours=1)
    db.session.execute(db.delete(OtpRequestEvent).where(OtpRequestEvent.occurred_at < since))
    db.session.add(OtpRequestEvent(request_ip=request_ip, occurred_at=now))
    count = db.session.scalar(
        select(db.func.count(OtpRequestEvent.id)).where(
            OtpRequestEvent.request_ip == request_ip,
            OtpRequestEvent.occurred_at >= since,
        )
    )
    if count > current_app.config["OTP_MAX_REQUESTS_PER_IP_PER_HOUR"]:
        db.session.rollback()
        raise AuthenticationError("Code request limit reached. Try again later.")
    db.session.commit()


def resend_otp(user_id: int, provider: OTPProvider, request_ip: str | None) -> None:
    user = db.session.get(User, user_id)
    if user is None or user.phone_verified_at is not None:
        raise AuthenticationError(OTP_GENERIC_ERROR)
    _create_challenge(user, provider, request_ip)
    db.session.commit()


def _create_challenge(user: User, provider: OTPProvider, request_ip: str | None) -> OtpChallenge:
    now = utcnow()
    cfg = current_app.config
    latest = db.session.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.phone == user.phone)
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    if latest and _as_utc(latest.created_at) > now - timedelta(
        seconds=cfg["OTP_RESEND_COOLDOWN_SECONDS"]
    ):
        raise AuthenticationError("Please wait before requesting another code.")
    since = now - timedelta(hours=1)
    send_count = db.session.scalar(
        select(db.func.count(OtpChallenge.id)).where(
            OtpChallenge.phone == user.phone, OtpChallenge.created_at >= since
        )
    )
    if send_count >= cfg["OTP_MAX_SENDS_PER_HOUR"]:
        raise AuthenticationError("Code request limit reached. Try again later.")
    if request_ip:
        ip_count = db.session.scalar(
            select(db.func.count(OtpChallenge.id)).where(
                OtpChallenge.request_ip == request_ip,
                OtpChallenge.created_at >= since,
            )
        )
        if ip_count >= cfg["OTP_MAX_REQUESTS_PER_IP_PER_HOUR"]:
            raise AuthenticationError("Code request limit reached. Try again later.")

    db.session.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.phone == user.phone,
            OtpChallenge.invalidated_at.is_(None),
            OtpChallenge.used_at.is_(None),
        )
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )
    challenge_id = secrets.token_hex(16)
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge = OtpChallenge(
        id=challenge_id,
        user_id=user.id,
        phone=user.phone,
        code_hmac=_code_hmac(challenge_id, user.phone, code),
        created_at=now,
        expires_at=now + timedelta(seconds=cfg["OTP_TTL_SECONDS"]),
        request_ip=request_ip,
    )
    db.session.add(challenge)
    db.session.flush()
    try:
        provider.send(user.phone, code)
    except OTPDeliveryError as exc:
        db.session.rollback()
        raise AuthenticationError(
            "We could not send a verification code. Please try again later."
        ) from exc
    return challenge


def verify_otp(user_id: int, code: str) -> None:
    now = utcnow()
    challenge = db.session.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.user_id == user_id)
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    if challenge is None:
        raise AuthenticationError(OTP_GENERIC_ERROR)
    maximum = current_app.config["OTP_MAX_ATTEMPTS"]
    result = db.session.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.id == challenge.id,
            OtpChallenge.invalidated_at.is_(None),
            OtpChallenge.used_at.is_(None),
            OtpChallenge.expires_at > now,
            OtpChallenge.attempt_count < maximum,
        )
        .values(attempt_count=OtpChallenge.attempt_count + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise AuthenticationError(OTP_GENERIC_ERROR)
    db.session.refresh(challenge)

    expected = _code_hmac(challenge.id, challenge.phone, code)
    if not hmac.compare_digest(expected, challenge.code_hmac):
        db.session.commit()
        raise AuthenticationError(OTP_GENERIC_ERROR)

    user = db.session.get(User, user_id)
    if user is None or not user.is_active or user.is_suspended:
        db.session.rollback()
        raise AuthenticationError(OTP_GENERIC_ERROR)
    challenge.used_at = now
    user.phone_verified_at = now
    db.session.commit()


def choose_role(user_id: int, role: str) -> User:
    if role not in {"FARMER", "BUYER"}:
        raise AuthenticationError("Choose Farmer or Buyer.")
    user = db.session.get(User, user_id)
    if (
        user is None
        or user.phone_verified_at is None
        or user.role is not None
        or not user.is_active
        or user.is_suspended
    ):
        raise AuthenticationError("Role selection is unavailable for this account.")
    user.role = role
    db.session.commit()
    return user


def create_admin_user(phone: str, password: str) -> User:
    """Create an already verified administrator for the trusted CLI workflow."""
    normalized_phone = normalize_phone(phone)
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthenticationError("Password must be at least 10 characters.")
    if db.session.scalar(select(User.id).where(User.phone == normalized_phone)):
        raise AuthenticationError("An account already uses that phone number.")
    user = User(phone=normalized_phone, role="ADMIN", phone_verified_at=utcnow())
    user.set_password(password)
    user.profile = Profile()
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise AuthenticationError("An account already uses that phone number.") from exc
    return user


def authenticate(phone: str, password: str) -> User:
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        raise AuthenticationError("Phone number or password is incorrect.") from None
    user = db.session.scalar(select(User).where(User.phone == normalized))
    hash_to_check = user.password_hash if user else _DUMMY_PASSWORD_HASH
    password_matches = check_password_hash(hash_to_check, password)
    now = utcnow()
    if user is None or not password_matches:
        if user:
            _record_failed_login(user, now)
            db.session.commit()
        raise AuthenticationError("Phone number or password is incorrect.")
    if user.locked_until and _as_utc(user.locked_until) > now:
        raise AuthenticationError("Phone number or password is incorrect.")
    if not user.is_active or user.is_suspended:
        raise AuthenticationError("Phone number or password is incorrect.")
    user.failed_login_attempts = 0
    user.locked_until = None
    db.session.commit()
    return user


def _record_failed_login(user: User, now: datetime) -> None:
    """Increment a failed-login counter atomically and apply account lockout."""
    if user.locked_until and _as_utc(user.locked_until) > now:
        return
    if user.locked_until:
        db.session.execute(
            update(User)
            .where(User.id == user.id, User.locked_until <= now)
            .values(failed_login_attempts=0, locked_until=None)
            .execution_options(synchronize_session=False)
        )
    increment = db.session.execute(
        update(User)
        .where(User.id == user.id, User.locked_until.is_(None))
        .values(failed_login_attempts=User.failed_login_attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if increment.rowcount:
        db.session.refresh(user)
        if user.failed_login_attempts >= LOGIN_FAILURE_LIMIT:
            db.session.execute(
                update(User)
                .where(User.id == user.id, User.locked_until.is_(None))
                .values(locked_until=now + timedelta(seconds=LOGIN_LOCK_SECONDS))
                .execution_options(synchronize_session=False)
            )
    db.session.refresh(user)


def _code_hmac(challenge_id: str, phone: str, code: str) -> str:
    pepper = current_app.config.get("OTP_PEPPER")
    if not pepper:
        raise RuntimeError("OTP_PEPPER must be configured")
    payload = f"{challenge_id}:{phone}:{code}".encode()
    return hmac.new(pepper.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
