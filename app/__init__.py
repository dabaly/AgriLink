"""AgriLink application factory."""

from __future__ import annotations

import logging
import os
from logging.config import dictConfig
from pathlib import Path

from flask import Flask, render_template

from app.config import config_by_name
from app.extensions import csrf, limiter, login_manager, migrate, socketio
from app.security import register_security_headers


def create_app(config_name: str | None = None, *, test_config: dict | None = None) -> Flask:
    """Create and configure an AgriLink application instance."""
    app = Flask(__name__, instance_relative_config=True)
    selected_config = config_name or os.environ.get("AGRI_LINK_CONFIG", "development")
    app.config["AGRI_LINK_CONFIG"] = selected_config
    app.config.from_object(config_by_name.get(selected_config, config_by_name["development"]))
    if selected_config == "production":
        missing = [name for name in ("SECRET_KEY", "OTP_PEPPER") if not app.config.get(name)]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} must be set when AGRI_LINK_CONFIG=production")
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    configure_logging(app)

    from app.extensions import db

    db.init_app(app)
    from app import models  # noqa: F401
    from app.auth.services import load_user

    login_manager.user_loader(load_user)

    with app.app_context():
        configure_sqlite(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(
        app,
        async_mode="threading",
        cors_allowed_origins=app.config["SOCKETIO_CORS_ALLOWED_ORIGINS"],
    )
    register_security_headers(app)

    from app.errors import register_error_handlers

    register_error_handlers(app)

    from app.cli import register_cli

    register_cli(app)

    from app.account import account_bp
    from app.auth import auth_bp
    from app.auth.services import get_otp_provider

    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)
    app.extensions["agri_link.otp_provider"] = get_otp_provider(app)

    @app.get("/")
    def home():
        return render_template("home.html")

    return app


def configure_logging(app: Flask) -> None:
    """Configure concise application logging without exposing secrets."""
    if app.testing:
        return
    log_path = Path(app.instance_path) / "agri_link.log"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}
            },
            "handlers": {
                "console": {"class": "logging.StreamHandler", "formatter": "standard"},
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": log_path,
                    "maxBytes": 1_000_000,
                    "backupCount": 3,
                    "formatter": "standard",
                    "encoding": "utf-8",
                },
            },
            "root": {"level": logging.INFO, "handlers": ["console", "file"]},
        }
    )


def configure_sqlite(app: Flask) -> None:
    """Set SQLite operational defaults when SQLite is the configured backend."""
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        return

    from sqlalchemy import event

    from app.extensions import db

    engine = db.engine

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(connection, _record):
        if connection.__class__.__module__ != "sqlite3":
            return
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(app.config['SQLITE_BUSY_TIMEOUT_MS'])}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
