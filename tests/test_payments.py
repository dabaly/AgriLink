"""Trusted payment boundary, provider event idempotency, and authorization tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Conflict, NotFound

from app.extensions import db
from app.marketplace.services import seed_categories
from app.models import Payment, PaymentEvent
from app.orders.services import OrderService
from app.payments.providers import InvalidProviderEvent, ProviderEvent
from app.payments.services import PaymentService
from tests.test_orders import make_order, make_user


@pytest.fixture
def payment_parties(app):
    seed_categories()
    buyer = make_user("+254711111111", "BUYER")
    seller = make_user("+254722222222", "FARMER")
    other_buyer = make_user("+254733333333", "BUYER")
    _listing, order = make_order(buyer, seller)
    return buyer, seller, other_buyer, order


def test_payment_initiation_is_scoped_and_amount_is_server_owned(payment_parties):
    buyer, _seller, other, order = payment_parties
    with pytest.raises(NotFound):
        PaymentService.initiate(other, order.id)
    payment, checkout = PaymentService.initiate(buyer, order.id)
    assert checkout is None
    assert payment.status == "PENDING"
    assert payment.amount_minor == order.total_minor == 62_500
    assert payment.currency == "KES"
    assert order.status == "CONFIRMED"
    assert PaymentService.initiate(buyer, order.id)[0].id == payment.id


def test_payment_only_becomes_paid_through_signed_mock_event(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    provider = PaymentService.provider("mock")
    body, signature = provider.simulated_event(payment, "success")
    event = PaymentService.process_webhook("mock", body, {"X-AgriLink-Signature": signature})
    db.session.refresh(order)
    db.session.refresh(payment)
    assert event.processing_status == "PROCESSED"
    assert payment.status == "SUCCEEDED"
    assert order.status == "PAID"
    assert [row.status for row in order.history] == ["CONFIRMED", "PAID"]


def test_invalid_signature_duplicate_and_amount_mismatch_are_safe(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    provider = PaymentService.provider("mock")
    body, signature = provider.simulated_event(payment, "success")
    with pytest.raises(InvalidProviderEvent):
        PaymentService.process_webhook("mock", body, {"X-AgriLink-Signature": "bad"})
    first = PaymentService.process_webhook("mock", body, {"X-AgriLink-Signature": signature})
    second = PaymentService.process_webhook("mock", body, {"X-AgriLink-Signature": signature})
    assert first.id == second.id
    assert db.session.scalar(select(db.func.count(PaymentEvent.id))) == 1
    assert order.status == "PAID"


def test_failed_payment_keeps_order_confirmed_and_can_retry(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    PaymentService.simulate_mock_event(payment.id, "failed")
    db.session.refresh(order)
    assert order.status == "CONFIRMED"
    assert payment.status == "FAILED"
    retry, _ = PaymentService.initiate(buyer, order.id)
    assert retry.id != payment.id


def test_nonconfirmed_and_alien_payment_access_are_hidden(payment_parties):
    buyer, _seller, other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    with pytest.raises(NotFound):
        PaymentService.query_status(other, payment.id)
    order.status = "CANCELLED"
    db.session.commit()
    with pytest.raises(Conflict):
        PaymentService.initiate(buyer, order.id)


def test_payment_constraints_prevent_two_active_or_successful_attempts(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    second = Payment(
        order_id=order.id,
        provider="MOCK",
        amount_minor=order.total_minor,
        currency="KES",
        status="PENDING",
        idempotency_key="second-attempt",
    )
    db.session.add(second)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Payment, payment.id) is not None


def test_refund_boundary_waits_for_confirmation(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    with pytest.raises(NotFound):
        PaymentService.refund(buyer, payment.id)
    PaymentService.simulate_mock_event(payment.id, "success")
    admin = make_user("+254799999999", "ADMIN")
    refund = PaymentService.refund(admin, payment.id)
    assert refund.refund_pending is True
    event = ProviderEvent(
        "refund-confirmed",
        "refund.confirmed",
        payment.provider_reference,
        "REFUNDED",
        payment.amount_minor,
        "KES",
        "mock-refund",
    )
    PaymentService.process_event("MOCK", event, payload=b"confirmed refund")
    db.session.refresh(order)
    db.session.refresh(payment)
    assert payment.status == "REFUNDED"
    assert order.status == "CANCELLED"


def test_stripe_signature_is_verified_without_network(app):
    import hashlib
    import hmac
    import json
    import time

    from app.payments.providers import StripePaymentProvider

    provider = StripePaymentProvider("sk_test_placeholder", "whsec_test", "https://example.test")
    body = json.dumps(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "payment_status": "paid",
                    "amount_total": 62500,
                    "currency": "kes",
                    "payment_intent": "pi_1",
                }
            },
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    event = provider.parse_webhook(body, {"Stripe-Signature": f"t={timestamp},v1={signature}"})
    assert event.status == "SUCCEEDED" and event.amount_minor == 62500
    with pytest.raises(InvalidProviderEvent):
        provider.parse_webhook(body, {"Stripe-Signature": "t=1,v1=bad"})


def test_provider_amount_mismatch_is_recorded_without_marking_paid(payment_parties):
    buyer, _seller, _other, order = payment_parties
    payment, _ = PaymentService.initiate(buyer, order.id)
    event = ProviderEvent(
        "mismatched",
        "payment.succeeded",
        payment.provider_reference,
        "SUCCEEDED",
        payment.amount_minor - 100,
        "KES",
    )
    result = PaymentService.process_event("MOCK", event, payload=b"mismatch")
    db.session.refresh(order)
    assert result.processing_status == "ACTION_REQUIRED"
    assert order.status == "CONFIRMED"


def test_mpesa_stk_callback_normalizes_result_without_float_math():
    import json

    from app.payments.providers import MpesaPaymentProvider

    provider = MpesaPaymentProvider({"MPESA_ENV": "sandbox"})
    body = json.dumps(
        {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": "ws_CO_123",
                    "ResultCode": 0,
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 625.50},
                            {"Name": "MpesaReceiptNumber", "Value": "ABC123"},
                        ]
                    },
                }
            }
        }
    ).encode()
    event = provider.parse_webhook(body, {})
    assert (event.reference, event.status, event.amount_minor, event.currency) == (
        "ws_CO_123",
        "SUCCEEDED",
        62_550,
        "KES",
    )


def test_mpesa_reversal_callback_maps_to_refund_event():
    import json

    from app.payments.providers import MpesaPaymentProvider

    provider = MpesaPaymentProvider({"MPESA_ENV": "sandbox"})
    body = json.dumps(
        {
            "Result": {
                "ResultCode": 0,
                "ConversationID": "conv-refund",
                "OriginatorConversationID": "origin-1",
                "ResultParameters": {
                    "ResultParameter": [
                        {"Key": "TransactionReceipt", "Value": "REV123"},
                    ]
                },
            }
        }
    ).encode()
    event = provider.parse_webhook(body, {})
    assert event.reference == "conv-refund"
    assert event.status == "REFUNDED"
    assert event.event_type == "refund.confirmed"


def test_mpesa_callback_rejects_fractional_minor_units():
    import json

    from app.payments.providers import MpesaPaymentProvider

    provider = MpesaPaymentProvider({"MPESA_ENV": "sandbox"})
    body = json.dumps(
        {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": "ws_CO_fraction",
                    "ResultCode": 0,
                    "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 625.501}]},
                }
            }
        }
    ).encode()
    with pytest.raises(InvalidProviderEvent):
        provider.parse_webhook(body, {})


def test_trusted_payment_enables_seller_fulfillment(payment_parties):
    buyer, seller, _other, order = payment_parties
    OrderService.save_delivery(buyer, order.id, {"method": "PICKUP"})
    payment, _ = PaymentService.initiate(buyer, order.id)
    PaymentService.simulate_mock_event(payment.id, "success")
    result = OrderService.transition(seller, order.id, "start_processing")
    assert result.status == "PROCESSING"


def test_browser_return_does_not_confirm_payment(payment_parties, client):
    buyer, _seller, _other, order = payment_parties
    from tests.test_chat import login_client

    login_client(client, buyer)
    response = client.get(f"/orders/{order.id}?payment=return")
    assert response.status_code == 200
    db.session.refresh(order)
    assert order.status == "CONFIRMED"


def test_stripe_checkout_initiation_uses_server_amount(monkeypatch):
    from types import SimpleNamespace

    from app.payments.providers import StripePaymentProvider

    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    def request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return Response()

    monkeypatch.setattr("app.payments.providers.requests.request", request)
    provider = StripePaymentProvider("sk_test", "whsec_test", "https://shop.example")
    payment = SimpleNamespace(id=3, idempotency_key="idem-1", amount_minor=62500, currency="KES")
    order = SimpleNamespace(id=9)
    result = provider.initiate(payment, order, SimpleNamespace())
    assert result.reference == "cs_test_1"
    assert seen["data"]["line_items[0][price_data][unit_amount]"] == "62500"
    assert seen["data"]["line_items[0][price_data][currency]"] == "kes"
    assert seen["headers"]["Idempotency-Key"] == "idem-1"


def test_mpesa_stk_initiation_maps_whole_kes_and_server_phone(monkeypatch):
    from types import SimpleNamespace

    from app.payments.providers import MpesaPaymentProvider

    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            if "oauth" in seen.get("url", ""):
                return {"access_token": "ephemeral"}
            return {"ResponseCode": "0", "CheckoutRequestID": "checkout-1"}

    def get(url, **kwargs):
        seen["url"] = url
        return Response()

    def post(url, **kwargs):
        seen.update(url=url, post_url=url, payload=kwargs["json"])
        return Response()

    monkeypatch.setattr("app.payments.providers.requests.get", get)
    monkeypatch.setattr("app.payments.providers.requests.post", post)
    config = {
        "MPESA_ENV": "sandbox",
        "MPESA_CONSUMER_KEY": "key",
        "MPESA_CONSUMER_SECRET": "secret",
        "MPESA_SHORTCODE": "123456",
        "MPESA_PASSKEY": "pass",
        "MPESA_CALLBACK_TOKEN": "random-token",
        "PAYMENT_PUBLIC_BASE_URL": "https://pay.example",
        "MPESA_TRANSACTION_TYPE": "CustomerPayBillOnline",
    }
    provider = MpesaPaymentProvider(config)
    payment = SimpleNamespace(amount_minor=62500)
    order = SimpleNamespace(id=9)
    buyer = SimpleNamespace(phone="+254711111111")
    result = provider.initiate(payment, order, buyer)
    assert result.reference == "checkout-1"
    assert seen["payload"]["Amount"] == 625
    assert seen["payload"]["PhoneNumber"] == "254711111111"
    assert seen["payload"]["CallBackURL"].endswith("/random-token")


def test_only_tokenized_mpesa_callback_route_is_exempt_and_accepted(payment_parties, app, client):
    import json

    buyer, _seller, _other, order = payment_parties
    import secrets

    callback_token = secrets.token_urlsafe(24)
    consumer_secret = secrets.token_urlsafe(32)
    app.config.update(
        MPESA_CALLBACK_TOKEN=callback_token,
        MPESA_CONSUMER_KEY="test-consumer",
        MPESA_CONSUMER_SECRET=consumer_secret,
        MPESA_SHORTCODE="123456",
        MPESA_PASSKEY="pass",
        PAYMENT_PUBLIC_BASE_URL="https://pay.example",
    )
    payment = Payment(
        order_id=order.id,
        provider="MPESA",
        provider_reference="checkout-ref",
        amount_minor=order.total_minor,
        currency="KES",
        status="PENDING",
        idempotency_key="mpesa-callback-test",
    )
    db.session.add(payment)
    db.session.commit()
    body = json.dumps(
        {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": "checkout-ref",
                    "ResultCode": 0,
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 625},
                            {"Name": "MpesaReceiptNumber", "Value": "MPESA123"},
                        ]
                    },
                }
            }
        }
    )
    assert client.post("/payments/webhooks/mpesa", data=body).status_code == 404
    assert client.post("/payments/webhooks/mpesa/wrong", data=body).status_code == 404
    response = client.post(f"/payments/webhooks/mpesa/{callback_token}", data=body)
    assert response.status_code == 204
    db.session.refresh(order)
    assert order.status == "PAID"


def test_payment_initiation_post_requires_csrf(client):
    response = client.post("/payments/orders/1/initiate")
    assert response.status_code == 400
