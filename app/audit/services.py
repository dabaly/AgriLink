"""Append-only audit write and admin query boundary."""

from __future__ import annotations

import json

from app.extensions import db
from app.models import AuditLog, User

ALLOWED_ACTIONS = {
    "USER_SUSPENDED",
    "USER_REACTIVATED",
    "LISTING_FLAGGED",
    "LISTING_APPROVED",
    "LISTING_REMOVED",
    "DISPUTE_MOVED_TO_REVIEW",
    "DISPUTE_RESOLVED_FOR_BUYER",
    "DISPUTE_RESOLVED_FOR_SELLER",
}


def append(*, actor: User | None, action: str, target_type: str, target_id: int, metadata=None):
    if actor is not None:
        from app.admin_ops.services import require_admin

        require_admin(actor)
    if action not in ALLOWED_ACTIONS or target_type not in {"user", "listing", "dispute"}:
        raise ValueError("Unsupported audit record.")
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id < 1:
        raise ValueError("Invalid audit target.")
    encoded = None
    if metadata is not None:
        if not isinstance(metadata, dict) or len(metadata) > 12:
            raise ValueError("Audit metadata must be a small object.")
        encoded = json.dumps(metadata, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 1000:
            raise ValueError("Audit metadata is too large.")
    record = AuditLog(
        actor_user_id=actor.id if actor else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata_json=encoded,
    )
    db.session.add(record)
    return record


class AuditService:
    append = staticmethod(append)
