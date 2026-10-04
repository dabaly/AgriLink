"""Application configuration."""

from __future__ import annotations

import os
from pathlib import Path


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "development-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{Path(__file__).resolve().parent.parent / 'instance' / 'agri_link.sqlite3'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLITE_BUSY_TIMEOUT_MS = 5000
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = 8 * 60 * 60
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_HEADERS_ENABLED = True
    SOCKETIO_CORS_ALLOWED_ORIGINS: list[str] = []
    JSON_SORT_KEYS = False
    OTP_PROVIDER = os.environ.get("OTP_PROVIDER", "mock")
    OTP_PEPPER = os.environ.get("OTP_PEPPER") or SECRET_KEY
    OTP_TTL_SECONDS = 300
    OTP_MAX_ATTEMPTS = 5
    OTP_RESEND_COOLDOWN_SECONDS = 60
    OTP_MAX_SENDS_PER_HOUR = 5
    OTP_MAX_REQUESTS_PER_IP_PER_HOUR = 20
    AT_USERNAME = os.environ.get("AT_USERNAME", "")
    AT_API_KEY = os.environ.get("AT_API_KEY", "")
    AT_SENDER_ID = os.environ.get("AT_SENDER_ID", "")
    AUTH_DUMMY_PASSWORD_HASH = None
    PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "mock")
    PAYMENT_MOCK_SECRET = os.environ.get("PAYMENT_MOCK_SECRET", "")
    PAYMENT_PUBLIC_BASE_URL = os.environ.get("PAYMENT_PUBLIC_BASE_URL", "http://localhost:5000")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    MPESA_ENV = os.environ.get("MPESA_ENV", "sandbox")
    MPESA_CONSUMER_KEY = os.environ.get("MPESA_CONSUMER_KEY", "")
    MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
    MPESA_SHORTCODE = os.environ.get("MPESA_SHORTCODE", "")
    MPESA_PASSKEY = os.environ.get("MPESA_PASSKEY", "")
    MPESA_TRANSACTION_TYPE = os.environ.get("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline")
    MPESA_CALLBACK_TOKEN = os.environ.get("MPESA_CALLBACK_TOKEN", "")
    MPESA_CALLBACK_IP_ALLOWLIST = tuple(
        filter(None, os.environ.get("MPESA_CALLBACK_IP_ALLOWLIST", "").split(","))
    )
    MPESA_INITIATOR_NAME = os.environ.get("MPESA_INITIATOR_NAME", "")
    MPESA_SECURITY_CREDENTIAL = os.environ.get("MPESA_SECURITY_CREDENTIAL", "")
    MPESA_REVERSAL_RESULT_URL = os.environ.get("MPESA_REVERSAL_RESULT_URL", "")
    MPESA_REVERSAL_TIMEOUT_URL = os.environ.get("MPESA_REVERSAL_TIMEOUT_URL", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_ENABLED = False
    SECRET_KEY = os.urandom(32)


class ProductionConfig(BaseConfig):
    DEBUG = False
    SECRET_KEY = os.environ.get("SECRET_KEY")
    OTP_PEPPER = os.environ.get("OTP_PEPPER")
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
