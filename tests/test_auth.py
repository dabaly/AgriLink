"""Authentication, OTP, authorization, and security regression tests."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.services import OTP_GENERIC_ERROR, AuthenticationError, record_otp_ip_request
from app.extensions import db
from app.integrations.otp import MockOTPProvider
from app.models import OtpChallenge, User

PHONE = "+254712345678"
PASSWORD = "a-long-safe-passphrase"  # noqa: S105


def csrf_token(response):
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data)
    assert match, response.data.decode()
    return match.group(1).decode()


def register(client, phone=PHONE, password=PASSWORD, confirm=None):
    page = client.get("/auth/register")
    return client.post(
        "/auth/register",
        data={
            "csrf_token": csrf_token(page),
            "phone": phone,
            "password": password,
            "confirm_password": confirm if confirm is not None else password,
        },
        follow_redirects=False,
    )


def complete_registration(client, app, role="BUYER"):
    response = register(client)
    assert response.status_code == 302
    provider = app.extensions["agri_link.otp_provider"]
    code = provider.sent_messages[-1][1]
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    verify_page = client.get("/auth/verify")
    response = client.post(
        "/auth/verify", data={"csrf_token": csrf_token(verify_page), "code": code}
    )
    assert response.status_code == 302
    role_page = client.get("/auth/choose-role")
    response = client.post(
        "/auth/choose-role", data={"csrf_token": csrf_token(role_page), "role": role}
    )
    assert response.status_code == 302
    return user


def test_registration_creates_user_and_profile_with_hashed_password(client, app):
    response = register(client, phone="0712345678")
    assert response.status_code == 302
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user is not None
    assert user.password_hash != PASSWORD
    assert user.check_password(PASSWORD)
    assert user.role is None
    assert user.phone_verified_at is None
    assert user.profile is not None
    assert user.profile.rating_count == 0
    assert user.profile.completed_orders_count == 0


@pytest.mark.parametrize(
    ("phone", "password", "confirm", "error"),
    [
        ("not-a-phone", PASSWORD, PASSWORD, b"valid phone number"),
        (PHONE, "short", "short", b"at least 10 characters"),
        (PHONE, PASSWORD, "different-password", b"Passwords do not match"),
    ],
)
def test_registration_rejects_invalid_input(client, phone, password, confirm, error):
    response = register(client, phone, password, confirm)
    assert response.status_code == 200
    assert error in response.data
    assert db.session.scalar(db.select(db.func.count(User.id))) == 0


def test_invalid_phone_login_fails_safely(client):
    page = client.get("/auth/login")
    response = client.post(
        "/auth/login",
        data={"csrf_token": csrf_token(page), "phone": "bad-number", "password": PASSWORD},
    )
    assert response.status_code == 200
    assert b"valid phone number and password" in response.data
    assert b"No account" not in response.data


def test_unverified_account_cannot_choose_role(client):
    register(client)
    response = client.get("/auth/choose-role")
    assert response.status_code == 302
    assert "/auth/login" in response.location
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user.role is None


def test_verified_account_can_resume_role_choice_after_session_loss(client, app):
    register(client)
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    verify_page = client.get("/auth/verify")
    client.post("/auth/verify", data={"csrf_token": csrf_token(verify_page), "code": code})
    with client.session_transaction() as session:
        csrf_seed = session.get("csrf_token")
        session.clear()
        session["csrf_token"] = csrf_seed
    page = client.get("/auth/login")
    response = client.post(
        "/auth/login",
        data={"csrf_token": csrf_token(page), "phone": PHONE, "password": PASSWORD},
    )
    assert response.location.endswith("/auth/choose-role")
    role_page = client.get("/auth/choose-role")
    client.post("/auth/choose-role", data={"csrf_token": csrf_token(role_page), "role": "FARMER"})
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user.role == "FARMER"
    assert client.get("/account/profile").status_code == 200


def test_registration_rejects_duplicate_phone(client):
    assert register(client).status_code == 302
    second = client.post(
        "/auth/register",
        data={
            "csrf_token": csrf_token(client.get("/auth/register")),
            "phone": PHONE,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    assert b"already uses that phone number" in second.data
    assert db.session.scalar(db.select(db.func.count(User.id))) == 1


def test_csrf_is_required_for_registration(client):
    response = client.post(
        "/auth/register",
        data={"phone": PHONE, "password": PASSWORD, "confirm_password": PASSWORD},
    )
    assert response.status_code == 400


def test_otp_is_six_digit_and_only_hmac_is_persisted(client, app):
    register(client)
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    challenge = db.session.scalar(db.select(OtpChallenge))
    assert len(code) == 6 and code.isdigit()
    assert code not in challenge.code_hmac
    assert len(challenge.code_hmac) == 64
    assert challenge.expires_at.replace(tzinfo=UTC) - challenge.created_at.replace(
        tzinfo=UTC
    ) == timedelta(minutes=5)


def test_wrong_otp_increments_attempts_and_success_verifies_phone(client, app):
    register(client)
    page = client.get("/auth/verify")
    token = csrf_token(page)
    wrong_code = (
        "000000"
        if app.extensions["agri_link.otp_provider"].sent_messages[-1][1] != "000000"
        else "000001"
    )
    wrong = client.post("/auth/verify", data={"csrf_token": token, "code": wrong_code})
    assert OTP_GENERIC_ERROR.encode() in wrong.data
    challenge = db.session.scalar(db.select(OtpChallenge))
    assert challenge.attempt_count == 1
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    response = client.post("/auth/verify", data={"csrf_token": token, "code": code})
    assert response.status_code == 302
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user.phone_verified_at is not None
    assert challenge.used_at is not None
    replay = client.post("/auth/verify", data={"csrf_token": token, "code": code})
    assert replay.status_code == 302
    with pytest.raises(ValueError, match="invalid or expired"):
        from app.auth.services import verify_otp

        verify_otp(user.id, code)


def test_fifth_failed_attempt_blocks_sixth(client, app):
    register(client)
    token = csrf_token(client.get("/auth/verify"))
    actual_code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    wrong_code = "000000" if actual_code != "000000" else "000001"
    for expected in range(1, 6):
        client.post("/auth/verify", data={"csrf_token": token, "code": wrong_code})
        challenge = db.session.scalar(db.select(OtpChallenge))
        assert challenge.attempt_count == expected
    client.post("/auth/verify", data={"csrf_token": token, "code": wrong_code})
    assert challenge.attempt_count == 5


def test_expired_otp_is_rejected(client, app):
    register(client)
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    challenge = db.session.scalar(db.select(OtpChallenge))
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.session.commit()
    response = client.post(
        "/auth/verify", data={"csrf_token": csrf_token(client.get("/auth/verify")), "code": code}
    )
    assert OTP_GENERIC_ERROR.encode() in response.data
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user.phone_verified_at is None


def test_resend_cooldown_and_new_code_invalidates_previous(client, app):
    register(client)
    provider = app.extensions["agri_link.otp_provider"]
    old_challenge = db.session.scalar(db.select(OtpChallenge))
    page = client.get("/auth/verify")
    client.post("/auth/resend-code", data={"csrf_token": csrf_token(page)})
    assert len(provider.sent_messages) == 1
    old_challenge.created_at = datetime.now(UTC) - timedelta(seconds=61)
    db.session.commit()
    page = client.get("/auth/verify")
    response = client.post(
        "/auth/resend-code", data={"csrf_token": csrf_token(page)}, follow_redirects=True
    )
    assert response.status_code == 200
    assert len(provider.sent_messages) == 2
    assert old_challenge.invalidated_at is not None
    assert db.session.scalar(db.select(db.func.count(OtpChallenge.id))) == 2


def test_hourly_phone_send_cap(client, app):
    register(client)
    for _ in range(4):
        challenge = db.session.scalar(
            db.select(OtpChallenge).order_by(OtpChallenge.created_at.desc()).limit(1)
        )
        challenge.created_at = datetime.now(UTC) - timedelta(seconds=61)
        db.session.commit()
        page = client.get("/auth/verify")
        client.post("/auth/resend-code", data={"csrf_token": csrf_token(page)})
    assert len(app.extensions["agri_link.otp_provider"].sent_messages) == 5
    challenge = db.session.scalar(
        db.select(OtpChallenge).order_by(OtpChallenge.created_at.desc()).limit(1)
    )
    challenge.created_at = datetime.now(UTC) - timedelta(seconds=61)
    db.session.commit()
    page = client.get("/auth/verify")
    response = client.post(
        "/auth/resend-code", data={"csrf_token": csrf_token(page)}, follow_redirects=True
    )
    assert b"request limit reached" in response.data
    assert len(app.extensions["agri_link.otp_provider"].sent_messages) == 5


@pytest.mark.parametrize("role", ["FARMER", "BUYER"])
def test_verified_user_can_choose_allowed_role(client, app, role):
    user = complete_registration(client, app, role)
    assert user.role == role
    assert client.get("/account/profile").status_code == 200
    assert role.title().encode() in client.get("/account/profile").data


def test_role_guard_requires_authentication_and_matching_role(client, app):
    from app.auth.decorators import role_required

    @role_required("FARMER")
    def farmer_view():
        return "farmer"

    app.add_url_rule("/test/farmer", view_func=farmer_view)
    assert client.get("/test/farmer").status_code == 302
    complete_registration(client, app, role="BUYER")
    assert client.get("/test/farmer").status_code == 404


def test_role_selection_rejects_admin_and_cannot_be_repeated(client, app):
    register(client)
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    verify_page = client.get("/auth/verify")
    client.post("/auth/verify", data={"csrf_token": csrf_token(verify_page), "code": code})
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    role_page = client.get("/auth/choose-role")
    rejected = client.post(
        "/auth/choose-role", data={"csrf_token": csrf_token(role_page), "role": "ADMIN"}
    )
    assert b"Not a valid choice" in rejected.data
    assert user.role is None
    role_page = client.get("/auth/choose-role")
    client.post("/auth/choose-role", data={"csrf_token": csrf_token(role_page), "role": "BUYER"})
    client.post("/auth/logout", data={"csrf_token": csrf_token(client.get("/account/profile"))})
    role_page = client.get("/auth/choose-role")
    assert role_page.status_code == 302
    assert user.role == "BUYER"


def test_login_success_logout_and_external_next_is_blocked(client, app):
    user = complete_registration(client, app)
    client.post("/auth/logout", data={"csrf_token": csrf_token(client.get("/account/profile"))})
    page = client.get("/auth/login?next=https://evil.example")
    response = client.post(
        "/auth/login?next=https://evil.example",
        data={"csrf_token": csrf_token(page), "phone": PHONE, "password": PASSWORD},
    )
    assert response.location.endswith("/account/profile")
    assert user.failed_login_attempts == 0
    assert client.get("/account/profile").status_code == 200
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    logout_page = client.get("/account/profile")
    out = client.post("/auth/logout", data={"csrf_token": csrf_token(logout_page)})
    assert out.status_code == 302
    assert client.get("/account/profile").status_code == 302


def test_login_failures_are_generic_and_lock_account(client, app):
    user = complete_registration(client, app)
    client.post("/auth/logout", data={"csrf_token": csrf_token(client.get("/account/profile"))})
    page = client.get("/auth/login")
    token = csrf_token(page)
    for _ in range(5):
        response = client.post(
            "/auth/login", data={"csrf_token": token, "phone": PHONE, "password": "wrong"}
        )
        assert b"incorrect" in response.data
    assert user.failed_login_attempts == 5
    assert user.locked_until is not None
    locked = client.post(
        "/auth/login", data={"csrf_token": token, "phone": PHONE, "password": PASSWORD}
    )
    assert b"incorrect" in locked.data


def test_unverified_and_suspended_users_cannot_login(client, app):
    register(client)
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    page = client.get("/auth/login")
    verification = client.post(
        "/auth/login", data={"csrf_token": csrf_token(page), "phone": PHONE, "password": PASSWORD}
    )
    assert verification.location.endswith("/auth/verify")
    assert not user.phone_verified_at
    code = app.extensions["agri_link.otp_provider"].sent_messages[-1][1]
    verify_page = client.get("/auth/verify")
    client.post("/auth/verify", data={"csrf_token": csrf_token(verify_page), "code": code})
    role_page = client.get("/auth/choose-role")
    client.post("/auth/choose-role", data={"csrf_token": csrf_token(role_page), "role": "BUYER"})
    client.post("/auth/logout", data={"csrf_token": csrf_token(client.get("/account/profile"))})
    user.is_suspended = True
    db.session.commit()
    page = client.get("/auth/login")
    response = client.post(
        "/auth/login", data={"csrf_token": csrf_token(page), "phone": PHONE, "password": PASSWORD}
    )
    assert b"incorrect" in response.data


def test_anonymous_cannot_access_account_and_public_registration_cannot_make_admin(client):
    response = client.get("/account/profile")
    assert response.status_code == 302 and "/auth/login" in response.location
    page = client.get("/auth/register")
    client.post(
        "/auth/register",
        data={
            "csrf_token": csrf_token(page),
            "phone": PHONE,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "role": "ADMIN",
        },
    )
    user = db.session.scalar(db.select(User).where(User.phone == PHONE))
    assert user.role is None


def test_admin_accounts_are_created_through_cli_only(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin"], input=f"0712345679\n{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    admin = db.session.scalar(db.select(User).where(User.phone == "+254712345679"))
    assert admin.role == "ADMIN"
    assert admin.phone_verified_at is not None
    assert admin.check_password(PASSWORD)


def test_password_and_otp_are_not_in_application_logs(client, app, caplog):
    register(client)
    provider = app.extensions["agri_link.otp_provider"]
    code = provider.sent_messages[-1][1]
    assert PASSWORD not in caplog.text
    assert code not in caplog.text


def test_development_mock_outbox_is_local_and_private(app, tmp_path):
    app.config["TESTING"] = False
    outbox = tmp_path / "dev_otp_outbox.log"
    with app.app_context():
        MockOTPProvider(outbox).send(PHONE, "123456")
    assert outbox.read_text(encoding="utf-8") == f"phone={PHONE} code=123456\n"
    assert outbox.stat().st_mode & 0o777 == 0o600


def test_login_rate_limiter_is_attached():
    from app import create_app

    rate_app = create_app("testing", test_config={"RATELIMIT_ENABLED": True})
    with rate_app.app_context():
        db.create_all()
        client = rate_app.test_client()
        client.environ_base["REMOTE_ADDR"] = "198.51.100.17"
        page = client.get("/auth/login")
        token = csrf_token(page)
        for _ in range(5):
            response = client.post(
                "/auth/login",
                data={"csrf_token": token, "phone": PHONE, "password": PASSWORD},
            )
            assert response.status_code == 200
        limited = client.post(
            "/auth/login", data={"csrf_token": token, "phone": PHONE, "password": PASSWORD}
        )
        assert limited.status_code == 429
        db.drop_all()


def test_otp_ip_quota_has_database_backing(app):
    for _ in range(20):
        record_otp_ip_request("203.0.113.9")
    with pytest.raises(AuthenticationError, match="request limit"):
        record_otp_ip_request("203.0.113.9")
