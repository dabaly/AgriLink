"""Payment orchestration, authorization, reconciliation, and state transitions."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db
from app.models import Order, Payment, PaymentEvent, User
from app.models.payment import PaymentStatus
from app.orders.services import OrderService
from app.payments.providers import (
    MockPaymentProvider,
    MpesaPaymentProvider,
    PaymentProviderError,
    StripePaymentProvider,
)


class PaymentService:
    ACTIVE_WINDOW = timedelta(minutes=10)

    @staticmethod
    def provider(name: str | None = None):
        config = current_app.config
        provider_name = (name or config.get("PAYMENT_PROVIDER", "mock")).lower()
        if (
            config.get("AGRI_LINK_CONFIG") == "production"
            and provider_name in {"stripe", "mpesa"}
            and not str(config.get("PAYMENT_PUBLIC_BASE_URL", "")).startswith("https://")
        ):
            raise PaymentProviderError("Live payment providers require a public HTTPS base URL.")
        cache_key = f"agri_link.payment_provider.{provider_name}"
        if cache_key in current_app.extensions:
            return current_app.extensions[cache_key]
        if provider_name == "mock":
            provider = MockPaymentProvider(
                config.get("PAYMENT_MOCK_SECRET") or config["SECRET_KEY"]
            )
        elif provider_name == "stripe":
            if not config.get("STRIPE_SECRET_KEY") or not config.get("STRIPE_WEBHOOK_SECRET"):
                raise PaymentProviderError("Stripe is not configured.")
            provider = StripePaymentProvider(
                config["STRIPE_SECRET_KEY"],
                config["STRIPE_WEBHOOK_SECRET"],
                config["PAYMENT_PUBLIC_BASE_URL"],
            )
        elif provider_name == "mpesa":
            required = (
                "MPESA_CONSUMER_KEY",
                "MPESA_CONSUMER_SECRET",
                "MPESA_SHORTCODE",
                "MPESA_PASSKEY",
                "MPESA_CALLBACK_TOKEN",
                "PAYMENT_PUBLIC_BASE_URL",
            )
            if any(not config.get(key) for key in required):
                raise PaymentProviderError("M-Pesa is not configured.")
            provider = MpesaPaymentProvider(config)
        else:
            raise PaymentProviderError("Unsupported payment provider.")
        current_app.extensions[cache_key] = provider
        return provider

    @staticmethod
    def _buyer_order(actor: User, order_id: int) -> Order:
        if (
            not actor
            or not actor.is_authenticated
            or actor.role != "BUYER"
            or actor.phone_verified_at is None
        ):
            raise NotFound()
        order = db.session.scalar(
            select(Order).where(Order.id == order_id, Order.buyer_id == actor.id)
        )
        if order is None:
            raise NotFound()
        return order

    @staticmethod
    def _payment(payment_id: int, *, actor: User | None = None) -> Payment:
        stmt = select(Payment).join(Order).where(Payment.id == payment_id)
        if actor is not None:
            if (
                not actor
                or not actor.is_authenticated
                or actor.phone_verified_at is None
                or actor.role not in {"BUYER", "FARMER"}
            ):
                raise NotFound()
            owner_col = Order.buyer_id if actor.role == "BUYER" else Order.seller_id
            stmt = stmt.where(owner_col == actor.id)
        payment = db.session.scalar(stmt)
        if payment is None:
            raise NotFound()
        return payment

    @classmethod
    def initiate(
        cls, actor: User, order_id: int, *, provider_name: str | None = None
    ) -> tuple[Payment, str | None]:
        order = cls._buyer_order(actor, order_id)
        if order.status != "CONFIRMED":
            raise Conflict("Only confirmed unpaid orders can be paid.")
        existing_success = db.session.scalar(
            select(Payment).where(
                Payment.order_id == order.id,
                Payment.status.in_([PaymentStatus.SUCCEEDED.value, PaymentStatus.REFUNDED.value]),
            )
        )
        if existing_success:
            raise Conflict("This order already has a successful payment.")
        provider = cls.provider(provider_name)
        now = datetime.now(UTC)
        active = db.session.scalar(
            select(Payment)
            .where(
                Payment.order_id == order.id,
                Payment.status.in_([PaymentStatus.INITIATED.value, PaymentStatus.PENDING.value]),
            )
            .order_by(Payment.created_at.desc())
        )
        if active:
            created_at = (
                active.created_at.replace(tzinfo=UTC)
                if active.created_at.tzinfo is None
                else active.created_at
            )
            if now - created_at <= cls.ACTIVE_WINDOW:
                return active, None
            # A stale attempt is only made retryable after a provider query says it ended.
            event = provider.query_status(active)
            if event is None or event.status == "PENDING":
                return active, None
            cls.process_event(active.provider, event, payload=b"trusted-status-query")
            db.session.refresh(order)
            if order.status != "CONFIRMED":
                raise Conflict("This order is no longer awaiting payment.")
            if event.status == "PENDING":
                return active, None
        payment = Payment(
            order_id=order.id,
            provider=provider.name,
            amount_minor=int(order.total_minor),
            currency="KES",
            status=PaymentStatus.INITIATED.value,
            idempotency_key=secrets.token_hex(24),
        )
        db.session.add(payment)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            active = db.session.scalar(
                select(Payment).where(
                    Payment.order_id == order.id,
                    Payment.status.in_(
                        [PaymentStatus.INITIATED.value, PaymentStatus.PENDING.value]
                    ),
                )
            )
            if active:
                return active, None
            raise Conflict("A payment attempt is already being created.") from None
        try:
            result = provider.initiate(payment, order, actor)
        except PaymentProviderError:
            payment.status = PaymentStatus.FAILED.value
            payment.failure_code = "PROVIDER_UNAVAILABLE"
            db.session.commit()
            raise
        payment.provider_reference = result.reference
        payment.status = PaymentStatus.PENDING.value
        db.session.commit()
        return payment, result.checkout_url

    @classmethod
    def process_webhook(cls, provider_name: str, body: bytes, headers) -> PaymentEvent:
        provider = cls.provider(provider_name)
        event = provider.parse_webhook(body, headers)
        return cls.process_event(provider.name, event, payload=body)

    @classmethod
    def process_event(cls, provider_name: str, event, *, payload: bytes = b"") -> PaymentEvent:
        digest = hashlib.sha256(payload).hexdigest()
        existing = db.session.scalar(
            select(PaymentEvent).where(
                PaymentEvent.provider == provider_name.upper(),
                PaymentEvent.provider_event_id == event.event_id,
            )
        )
        if existing:
            return existing
        payment = db.session.scalar(
            select(Payment).where(
                Payment.provider == provider_name.upper(),
                or_(
                    Payment.provider_reference == event.reference,
                    Payment.refund_reference == event.reference,
                    Payment.provider_resource_id == event.reference,
                    Payment.id == event.internal_payment_id
                    if event.internal_payment_id is not None
                    else Payment.id.is_(None),
                ),
            )
        )
        record = PaymentEvent(
            payment_id=payment.id if payment else None,
            order_id=payment.order_id if payment else None,
            provider=provider_name.upper(),
            provider_event_id=event.event_id,
            event_type=event.event_type[:80],
            payload_digest=digest,
            processing_status="REJECTED",
            result_message="Payment reference not found.",
            processed_at=datetime.now(UTC),
        )
        db.session.add(record)
        if payment is None:
            db.session.commit()
            return record
        order = db.session.get(Order, payment.order_id)
        mismatch = (
            (event.amount_minor is not None and event.amount_minor != payment.amount_minor)
            or (event.currency is not None and event.currency.upper() != "KES")
            or (
                event.status == "SUCCEEDED"
                and (event.amount_minor is None or event.currency is None)
            )
            or (event.internal_order_id is not None and event.internal_order_id != payment.order_id)
            or payment.currency != "KES"
            or (order is None or payment.amount_minor != order.total_minor)
        )
        if mismatch:
            record.processing_status = "ACTION_REQUIRED"
            record.result_message = "Provider amount or currency does not match this order."
            db.session.commit()
            return record
        if event.resource_reference and payment.provider_resource_id is None:
            payment.provider_resource_id = str(event.resource_reference)[:255]
        if event.status == "PENDING":
            if payment.status == PaymentStatus.INITIATED.value:
                payment.status = PaymentStatus.PENDING.value
            record.processing_status = "PROCESSED"
            record.result_message = "Payment remains pending."
        elif event.status in {"FAILED", "EXPIRED"}:
            if event.event_type.startswith("refund"):
                payment.refund_pending = False
                record.processing_status = "PROCESSED"
                record.result_message = "Refund failed; payment remains successful."
            elif payment.status not in {
                PaymentStatus.SUCCEEDED.value,
                PaymentStatus.REFUNDED.value,
            }:
                payment.status = event.status
                payment.failure_code = (event.failure_code or event.status)[:80]
                record.processing_status = "PROCESSED"
                record.result_message = f"Payment {event.status.lower()}."
        elif event.status == "SUCCEEDED":
            if payment.status in {PaymentStatus.SUCCEEDED.value, PaymentStatus.REFUNDED.value}:
                record.processing_status = "PROCESSED"
                record.result_message = "Payment was already confirmed."
            elif order.status == "CONFIRMED":
                payment.status = PaymentStatus.SUCCEEDED.value
                payment.completed_at = datetime.now(UTC)
                OrderService._mark_paid_after_trusted_payment(order.id)
                record.processing_status = "PROCESSED"
                record.result_message = "Payment confirmed and order marked paid."
            else:
                payment.status = PaymentStatus.SUCCEEDED.value
                payment.completed_at = datetime.now(UTC)
                record.processing_status = "ACTION_REQUIRED"
                record.result_message = (
                    "Payment confirmed after order state changed; review required."
                )
        elif event.status == "REFUNDED":
            if payment.status == PaymentStatus.REFUNDED.value:
                record.processing_status = "PROCESSED"
                record.result_message = "Refund was already confirmed."
            elif payment.status == PaymentStatus.SUCCEEDED.value:
                payment.status = PaymentStatus.REFUNDED.value
                payment.refund_pending = False
                payment.refunded_at = datetime.now(UTC)
                if order.status == "PAID":
                    OrderService._cancel_after_trusted_refund(order.id)
                record.processing_status = "PROCESSED"
                record.result_message = "Refund confirmed."
            else:
                record.processing_status = "ACTION_REQUIRED"
                record.result_message = "Refund confirmation does not match payment state."
        else:
            record.processing_status = "REJECTED"
            record.result_message = "Unsupported payment state."
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            duplicate = db.session.scalar(
                select(PaymentEvent).where(
                    PaymentEvent.provider == provider_name.upper(),
                    PaymentEvent.provider_event_id == event.event_id,
                )
            )
            if duplicate:
                return duplicate
            raise
        return record

    @classmethod
    def latest_for_order(cls, actor: User, order_id: int) -> Payment | None:
        cls._payment_scope(actor, order_id)
        return db.session.scalar(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc())
        )

    @staticmethod
    def _payment_scope(actor: User, order_id: int) -> Order:
        if (
            not actor
            or not actor.is_authenticated
            or actor.phone_verified_at is None
            or actor.role not in {"BUYER", "FARMER"}
        ):
            raise NotFound()
        col = Order.buyer_id if actor.role == "BUYER" else Order.seller_id
        order = db.session.scalar(select(Order).where(Order.id == order_id, col == actor.id))
        if order is None:
            raise NotFound()
        return order

    @classmethod
    def query_status(cls, actor: User, payment_id: int) -> Payment:
        payment = cls._payment(payment_id, actor=actor)
        event = cls.provider(payment.provider.lower()).query_status(payment)
        if event:
            cls.process_event(payment.provider, event, payload=b"trusted-status-query")
        db.session.refresh(payment)
        return payment

    @classmethod
    def refund(cls, actor: User, payment_id: int) -> Payment:
        if (
            not actor
            or not actor.is_authenticated
            or actor.role != "ADMIN"
            or actor.phone_verified_at is None
        ):
            raise NotFound()
        payment = cls._payment(payment_id)
        if payment.refund_pending:
            return payment
        claimed = db.session.execute(
            update(Payment)
            .where(
                Payment.id == payment_id,
                Payment.status == PaymentStatus.SUCCEEDED.value,
                Payment.refund_pending.is_(False),
                Payment.order.has(and_(Order.status == "PAID", Order.left_seller_at.is_(None))),
            )
            .values(refund_pending=True)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            db.session.rollback()
            raise Conflict("Only a paid order that has not left the seller can be refunded.")
        db.session.commit()
        try:
            result = cls.provider(payment.provider.lower()).refund(payment)
        except PaymentProviderError:
            payment = db.session.get(Payment, payment_id)
            payment.refund_pending = False
            db.session.commit()
            raise
        payment = db.session.get(Payment, payment_id)
        payment.refund_reference = result.reference
        db.session.commit()
        return payment

    @classmethod
    def simulate_mock_event(cls, payment_id: int, outcome: str) -> PaymentEvent:
        if current_app.config.get("AGRI_LINK_CONFIG") == "production":
            raise NotFound()
        payment = db.session.get(Payment, payment_id)
        if payment is None or payment.provider != "MOCK":
            raise NotFound()
        provider = cls.provider("mock")
        body, signature = provider.simulated_event(payment, outcome)
        return cls.process_webhook("mock", body, {"X-AgriLink-Signature": signature})
