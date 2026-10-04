"""Persisted one-time password challenges."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import utcnow


class OtpChallenge(db.Model):
    __tablename__ = "otp_challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_ip: Mapped[str | None] = mapped_column(String(45), index=True)

    user = relationship("User")

    @property
    def is_active(self) -> bool:
        now = utcnow()
        return (
            self.invalidated_at is None
            and self.used_at is None
            and self.expires_at.replace(tzinfo=now.tzinfo) > now
            and self.attempt_count < 5
        )


class OtpRequestEvent(db.Model):
    """Short-lived database-backed record for OTP IP request quotas."""

    __tablename__ = "otp_request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_ip: Mapped[str] = mapped_column(String(45), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
