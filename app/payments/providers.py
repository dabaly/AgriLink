"""Payment-provider adapters; provider responses are normalized before services see them."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests


class PaymentProviderError(RuntimeError):
    """A provider could not safely complete the requested operation."""


class InvalidProviderEvent(ValueError):
    """A provider event is malformed or its authentication failed."""


def _kes_minor(value) -> int:
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise InvalidProviderEvent("Provider amount is not an exact KES minor-unit value.")
    return int(amount)


@dataclass(frozen=True)
class ProviderInitiation:
    reference: str
    checkout_url: str | None = None
    message: str = "Payment request sent."


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    event_type: str
    reference: str
    status: str
    amount_minor: int | None = None
    currency: str | None = None
    resource_reference: str | None = None
    failure_code: str | None = None
    internal_payment_id: int | None = None
    internal_order_id: int | None = None


class PaymentProvider(Protocol):
    name: str

    def initiate(self, payment, order, buyer) -> ProviderInitiation: ...
    def parse_webhook(self, body: bytes, headers) -> ProviderEvent: ...
    def query_status(self, payment) -> ProviderEvent | None: ...
    def refund(self, payment) -> ProviderInitiation: ...


class MockPaymentProvider:
    name = "MOCK"

    def __init__(self, secret: str):
        self.secret = secret if isinstance(secret, bytes) else secret.encode()
        self.states: dict[str, ProviderEvent] = {}

    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def initiate(self, payment, order, buyer) -> ProviderInitiation:
        reference = f"mock-{payment.idempotency_key}"
        self.states.setdefault(
            reference,
            ProviderEvent(
                f"{reference}:pending",
                "payment.pending",
                reference,
                "PENDING",
                payment.amount_minor,
                payment.currency,
            ),
        )
        return ProviderInitiation(
            reference,
            message=(
                "Mock payment is pending. Use the development CLI to simulate a provider event."
            ),
        )

    def parse_webhook(self, body: bytes, headers) -> ProviderEvent:
        supplied = headers.get("X-AgriLink-Signature", "")
        if not hmac.compare_digest(supplied, self.sign(body)):
            raise InvalidProviderEvent("Invalid provider signature.")
        try:
            data = json.loads(body)
            event = ProviderEvent(
                str(data["event_id"]),
                str(data["event_type"]),
                str(data["reference"]),
                str(data["status"]).upper(),
                int(data["amount_minor"]),
                str(data["currency"]).upper(),
                data.get("resource_reference"),
                data.get("failure_code"),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidProviderEvent("Malformed provider event.") from exc
        if event.status not in {"PENDING", "SUCCEEDED", "FAILED", "EXPIRED", "REFUNDED"}:
            raise InvalidProviderEvent("Unsupported provider event status.")
        self.states[event.reference] = event
        return event

    def query_status(self, payment) -> ProviderEvent | None:
        return self.states.get(payment.provider_reference or "")

    def refund(self, payment) -> ProviderInitiation:
        reference = f"mock-refund-{payment.id}"
        self.states[reference] = ProviderEvent(
            f"{reference}:pending",
            "refund.pending",
            payment.provider_reference or reference,
            "PENDING",
            payment.amount_minor,
            payment.currency,
            reference,
        )
        return ProviderInitiation(reference, message="Mock refund pending confirmation.")

    def simulated_event(self, payment, outcome: str) -> tuple[bytes, str]:
        status = {
            "success": "SUCCEEDED",
            "succeeded": "SUCCEEDED",
            "failed": "FAILED",
            "expired": "EXPIRED",
            "pending": "PENDING",
            "refunded": "REFUNDED",
        }.get(outcome.lower())
        if status is None:
            raise ValueError("Outcome must be pending, success, failed, expired, or refunded.")
        if status == "REFUNDED" and not payment.refund_pending:
            raise ValueError("A mock refund event requires a pending refund request.")
        ref = payment.provider_reference or f"mock-{payment.idempotency_key}"
        if status == "REFUNDED" and payment.refund_reference:
            ref = payment.refund_reference
        data = {
            "event_id": f"{ref}:{status.lower()}:{payment.id}",
            "event_type": "refund.confirmed"
            if status == "REFUNDED"
            else f"payment.{status.lower()}",
            "reference": ref,
            "status": status,
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "resource_reference": f"mock-resource-{payment.id}",
        }
        body = json.dumps(data, separators=(",", ":")).encode()
        return body, self.sign(body)


class StripePaymentProvider:
    name = "STRIPE"
    api = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str, webhook_secret: str, base_url: str):
        self.secret_key, self.webhook_secret, self.base_url = (
            secret_key,
            webhook_secret,
            base_url.rstrip("/"),
        )

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = requests.request(
                method, self.api + path, auth=(self.secret_key, ""), timeout=15, **kwargs
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentProviderError("Stripe request failed.") from exc

    def initiate(self, payment, order, buyer) -> ProviderInitiation:
        data = self._request(
            "POST",
            "/checkout/sessions",
            data={
                "mode": "payment",
                "success_url": f"{self.base_url}/orders/{order.id}?payment=return",
                "cancel_url": f"{self.base_url}/orders/{order.id}?payment=cancelled",
                "client_reference_id": str(payment.id),
                "metadata[payment_id]": str(payment.id),
                "metadata[order_id]": str(order.id),
                "line_items[0][quantity]": "1",
                "line_items[0][price_data][currency]": payment.currency.lower(),
                "line_items[0][price_data][unit_amount]": str(payment.amount_minor),
                "line_items[0][price_data][product_data][name]": f"AgriLink order {order.id}",
            },
            headers={"Idempotency-Key": payment.idempotency_key},
        )
        checkout_url = data.get("url")
        if not data.get("id") or not checkout_url:
            raise PaymentProviderError("Stripe did not create a checkout session.")
        if urlparse(checkout_url or "").hostname != "checkout.stripe.com" or not (
            checkout_url or ""
        ).startswith("https://"):
            raise PaymentProviderError("Stripe returned an invalid checkout URL.")
        return ProviderInitiation(str(data["id"]), checkout_url, "Continue to Stripe Checkout.")

    def parse_webhook(self, body: bytes, headers) -> ProviderEvent:
        signature = headers.get("Stripe-Signature", "")
        pieces = {}
        for part in signature.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                pieces.setdefault(key, []).append(value)
        timestamps, signatures = pieces.get("t", []), pieces.get("v1", [])
        if not timestamps or not signatures:
            raise InvalidProviderEvent("Invalid Stripe signature.")
        timestamp = timestamps[0]
        if not timestamp.isdigit() or abs(int(time.time()) - int(timestamp)) > 300:
            raise InvalidProviderEvent("Expired Stripe signature.")
        signed = timestamp.encode() + b"." + body
        expected = hmac.new(self.webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, value) for value in signatures):
            raise InvalidProviderEvent("Invalid Stripe signature.")
        try:
            data = json.loads(body)
            obj = data["data"]["object"]
            etype, event_id = data["type"], data["id"]
            amount = None
            if "amount_total" in obj:
                amount = int(obj["amount_total"])
            elif etype == "charge.refunded" and "amount_refunded" in obj:
                amount = int(obj["amount_refunded"])
            currency = str(obj["currency"]).upper() if obj.get("currency") else None
            status = "PENDING"
            failure = None
            if etype in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
                status = (
                    "SUCCEEDED"
                    if obj.get("payment_status") == "paid" or "async_payment_succeeded" in etype
                    else "PENDING"
                )
            elif etype == "checkout.session.async_payment_failed":
                status, failure = "FAILED", "ASYNC_PAYMENT_FAILED"
            elif etype == "charge.refunded":
                status = "REFUNDED"
            ref = str(
                obj.get("payment_intent") if etype == "charge.refunded" else obj.get("id", "")
            )
            if not ref:
                raise KeyError("id")
            return ProviderEvent(
                str(event_id),
                str(etype),
                ref,
                status,
                amount,
                currency,
                obj.get("payment_intent"),
                failure,
                int(obj["metadata"]["payment_id"])
                if obj.get("metadata", {}).get("payment_id")
                else None,
                int(obj["metadata"]["order_id"])
                if obj.get("metadata", {}).get("order_id")
                else None,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidProviderEvent("Malformed Stripe event.") from exc

    def query_status(self, payment) -> ProviderEvent | None:
        if not payment.provider_reference:
            return None
        data = self._request("GET", f"/checkout/sessions/{payment.provider_reference}")
        status = "SUCCEEDED" if data.get("payment_status") == "paid" else "PENDING"
        if data.get("status") == "expired":
            status = "EXPIRED"
        return ProviderEvent(
            f"query:{data['id']}:{status}",
            "status.query",
            data["id"],
            status,
            int(data["amount_total"]) if data.get("amount_total") is not None else None,
            str(data.get("currency", "")).upper() or None,
            data.get("payment_intent"),
        )

    def refund(self, payment) -> ProviderInitiation:
        if not payment.provider_resource_id:
            raise PaymentProviderError("Stripe payment intent is not available for refund.")
        data = self._request(
            "POST",
            "/refunds",
            data={"payment_intent": payment.provider_resource_id},
            headers={"Idempotency-Key": f"refund-{payment.idempotency_key}"},
        )
        return ProviderInitiation(str(data["id"]), message="Refund submitted to Stripe.")


class MpesaPaymentProvider:
    name = "MPESA"

    def __init__(self, config):
        self.config = config
        self.base = (
            "https://sandbox.safaricom.co.ke"
            if config.get("MPESA_ENV") == "sandbox"
            else "https://api.safaricom.co.ke"
        )

    def _token(self):
        import base64

        credentials = (
            f"{self.config['MPESA_CONSUMER_KEY']}:{self.config['MPESA_CONSUMER_SECRET']}".encode()
        )
        try:
            response = requests.get(
                self.base + "/oauth/v1/generate?grant_type=client_credentials",
                headers={"Authorization": "Basic " + base64.b64encode(credentials).decode()},
                timeout=15,
            )
            response.raise_for_status()
            return response.json()["access_token"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise PaymentProviderError("M-Pesa authorization failed.") from exc

    def _post(self, path, data):
        try:
            response = requests.post(
                self.base + path,
                json=data,
                headers={"Authorization": "Bearer " + self._token()},
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentProviderError("M-Pesa request failed.") from exc

    def initiate(self, payment, order, buyer) -> ProviderInitiation:
        if payment.amount_minor % 100:
            raise PaymentProviderError("M-Pesa supports whole-KES payment amounts only.")
        phone = buyer.phone.lstrip("+")
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        elif phone.startswith("254"):
            pass
        else:
            raise PaymentProviderError("Buyer phone is not a Kenyan mobile number.")
        import base64
        from datetime import datetime

        stamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        shortcode = self.config["MPESA_SHORTCODE"]
        password = base64.b64encode(
            (shortcode + self.config["MPESA_PASSKEY"] + stamp).encode()
        ).decode()
        callback = (
            f"{self.config['PAYMENT_PUBLIC_BASE_URL'].rstrip('/')}"
            f"/payments/webhooks/mpesa/{self.config['MPESA_CALLBACK_TOKEN']}"
        )
        data = self._post(
            "/mpesa/stkpush/v1/processrequest",
            {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": stamp,
                "TransactionType": self.config.get(
                    "MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"
                ),
                "Amount": payment.amount_minor // 100,
                "PartyA": phone,
                "PartyB": shortcode,
                "PhoneNumber": phone,
                "CallBackURL": callback,
                "AccountReference": f"AGL-{order.id}",
                "TransactionDesc": f"AgriLink order {order.id}",
            },
        )
        if str(data.get("ResponseCode", "")) != "0" or not data.get("CheckoutRequestID"):
            raise PaymentProviderError("M-Pesa did not accept the STK Push request.")
        return ProviderInitiation(
            str(data["CheckoutRequestID"]),
            message="STK Push sent. Check your phone to complete payment.",
        )

    def parse_webhook(self, body: bytes, headers) -> ProviderEvent:
        try:
            root = json.loads(body, parse_float=Decimal)
            if "Result" in root:
                data = root["Result"]
                result = int(data["ResultCode"])
                reference = str(data.get("ConversationID") or data["OriginatorConversationID"])
                receipt = None
                amount = None
                for item in data.get("ResultParameters", {}).get("ResultParameter", []):
                    if item.get("Key") == "TransactionReceipt":
                        receipt = str(item.get("Value"))
                    if item.get("Key") == "Amount":
                        amount = _kes_minor(item.get("Value"))
                return ProviderEvent(
                    f"reversal:{reference}:{result}:{receipt or 'none'}",
                    "refund.confirmed" if result == 0 else "refund.failed",
                    reference,
                    "REFUNDED" if result == 0 else "FAILED",
                    amount,
                    "KES",
                    receipt,
                    None if result == 0 else f"MPESA_REVERSAL_{result}",
                )
            data = root["Body"]["stkCallback"]
            reference, result = str(data["CheckoutRequestID"]), int(data["ResultCode"])
            metadata = {
                str(x.get("Name")): x.get("Value")
                for x in data.get("CallbackMetadata", {}).get("Item", [])
            }
            amount = None
            if "Amount" in metadata:
                try:
                    amount = _kes_minor(metadata["Amount"])
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise InvalidProviderEvent("Invalid M-Pesa amount.") from exc
            receipt = str(metadata.get("MpesaReceiptNumber", "")) or None
            event_id = f"{reference}:{result}:{receipt or 'none'}"
            return ProviderEvent(
                event_id,
                "stk.callback",
                reference,
                "SUCCEEDED" if result == 0 else "FAILED",
                amount,
                "KES" if amount is not None else None,
                receipt,
                None if result == 0 else f"MPESA_{result}",
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidProviderEvent("Malformed M-Pesa callback.") from exc

    def query_status(self, payment) -> ProviderEvent | None:
        if not payment.provider_reference:
            return None
        import base64
        from datetime import datetime

        stamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        shortcode = self.config["MPESA_SHORTCODE"]
        password = base64.b64encode(
            (shortcode + self.config["MPESA_PASSKEY"] + stamp).encode()
        ).decode()
        data = self._post(
            "/mpesa/stkpushquery/v1/query",
            {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": stamp,
                "CheckoutRequestID": payment.provider_reference,
            },
        )
        result = int(data.get("ResultCode", -1))
        status = (
            "SUCCEEDED"
            if result == 0
            else "PENDING"
            if result == -1
            else "EXPIRED"
            if result == 1037
            else "FAILED"
        )
        return ProviderEvent(
            f"query:{payment.provider_reference}:{result}",
            "status.query",
            payment.provider_reference,
            status,
            payment.amount_minor if result == 0 else None,
            "KES" if result == 0 else None,
            failure_code=None if result in {0, -1} else f"MPESA_{result}",
        )

    def refund(self, payment) -> ProviderInitiation:
        if not payment.provider_resource_id:
            raise PaymentProviderError("M-Pesa receipt is not available for reversal.")
        cfg = self.config
        result = self._post(
            "/mpesa/reversal/v1/request",
            {
                "Initiator": cfg["MPESA_INITIATOR_NAME"],
                "SecurityCredential": cfg["MPESA_SECURITY_CREDENTIAL"],
                "CommandID": "TransactionReversal",
                "TransactionID": payment.provider_resource_id,
                "Amount": payment.amount_minor // 100,
                "ReceiverParty": cfg["MPESA_SHORTCODE"],
                "RecieverIdentifierType": "11",
                "ResultURL": cfg["MPESA_REVERSAL_RESULT_URL"],
                "QueueTimeOutURL": cfg["MPESA_REVERSAL_TIMEOUT_URL"],
                "Remarks": "AgriLink order refund",
                "Occasion": f"Order {payment.order_id}",
            },
        )
        if str(result.get("ResponseCode", "")) != "0":
            raise PaymentProviderError("M-Pesa did not accept the reversal request.")
        return ProviderInitiation(
            str(
                result.get("ConversationID")
                or result.get("OriginatorConversationID")
                or payment.provider_resource_id
            ),
            message="M-Pesa reversal submitted.",
        )
