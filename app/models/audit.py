"""Append-only administrative audit records."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.user import User, utcnow


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "action IN ('USER_SUSPENDED','USER_REACTIVATED','LISTING_FLAGGED',"
            "'LISTING_APPROVED','LISTING_REMOVED','DISPUTE_MOVED_TO_REVIEW',"
            "'DISPUTE_RESOLVED_FOR_BUYER','DISPUTE_RESOLVED_FOR_SELLER')",
            name="ck_audit_logs_action",
        ),
        CheckConstraint("target_type IN ('user','listing','dispute')", name="ck_audit_logs_target"),
        Index("ix_audit_logs_created", "created_at", "id"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    actor = relationship(User)


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _keep_audit_append_only(_mapper, _connection, _target) -> None:
    raise ValueError("Audit logs are append-only.")
