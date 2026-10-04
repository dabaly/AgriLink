"""OTP delivery provider interface and implementations."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class OTPDeliveryError(RuntimeError):
    """Raised when an OTP provider cannot deliver a message."""


class OTPProvider(ABC):
    @abstractmethod
    def send(self, phone: str, code: str) -> None:
        """Deliver a one-time verification code."""


class MockOTPProvider(OTPProvider):
    """Development/test provider. It writes codes only to a local dev outbox."""

    def __init__(self, outbox_path: str | Path | None = None):
        self.outbox_path = Path(outbox_path) if outbox_path else None
        self.sent_messages: list[tuple[str, str]] = []

    def send(self, phone: str, code: str) -> None:
        if current_app.config.get("AGRI_LINK_CONFIG") == "production":
            raise OTPDeliveryError("Mock OTP provider cannot run in production")
        self.sent_messages.append((phone, code))
        if current_app.testing:
            return
        path = self.outbox_path or Path(current_app.instance_path) / "dev_otp_outbox.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        path.chmod(0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as outbox:
            outbox.write(f"phone={phone} code={code}\n")
        logger.info("Development OTP written to %s for %s", path, phone)


class AfricaTalkingOTPProvider(OTPProvider):
    """Africa's Talking SMS adapter. No business state is managed here."""

    endpoint = "https://api.africastalking.com/version1/messaging"

    def send(self, phone: str, code: str) -> None:
        username = current_app.config.get("AT_USERNAME")
        api_key = current_app.config.get("AT_API_KEY")
        if not username or not api_key:
            raise OTPDeliveryError("Africa's Talking credentials are not configured")
        data = {
            "username": username,
            "to": phone,
            "message": f"Your AgriLink verification code is {code}.",
        }
        sender_id = current_app.config.get("AT_SENDER_ID")
        if sender_id:
            data["from"] = sender_id
        try:
            response = requests.post(
                self.endpoint,
                data=data,
                headers={"apiKey": api_key, "Accept": "application/json"},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise OTPDeliveryError("OTP delivery provider request failed") from exc
        except ValueError as exc:
            raise OTPDeliveryError("OTP delivery provider returned an invalid response") from exc
        recipients = payload.get("SMSMessageData", {}).get("Recipients", [])
        if not recipients or any(str(item.get("statusCode")) != "101" for item in recipients):
            raise OTPDeliveryError("OTP delivery provider rejected the message")
