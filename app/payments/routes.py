"""Buyer payment initiation and narrowly exempt provider callbacks."""

from __future__ import annotations

import hmac
import ipaddress

from flask import Blueprint, abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import Conflict, NotFound, UnprocessableEntity

from app.extensions import csrf
from app.payments.providers import InvalidProviderEvent, PaymentProviderError
from app.payments.services import PaymentService

payments_bp = Blueprint("payments", __name__, url_prefix="/payments")


@payments_bp.post("/orders/<int:order_id>/initiate")
@login_required
def initiate(order_id: int):
    try:
        payment, checkout_url = PaymentService.initiate(current_user, order_id)
    except NotFound:
        abort(404)
    except (Conflict, UnprocessableEntity) as exc:
        flash(str(exc), "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    except PaymentProviderError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    if checkout_url:
        return redirect(checkout_url, code=303)
    if payment.provider == "MOCK":
        flash(
            f"Mock payment #{payment.id} is pending. Use the development CLI to simulate an event.",
            "info",
        )
    elif payment.provider == "MPESA":
        flash("Payment request sent. Check your phone to complete the STK Push.", "info")
    else:
        flash("Payment is pending provider confirmation.", "info")
    return redirect(url_for("orders.detail", order_id=order_id))


@payments_bp.post("/webhooks/<provider>")
@csrf.exempt
def webhook(provider: str):
    return _handle_webhook(provider)


@payments_bp.post("/webhooks/mpesa/<token>")
@csrf.exempt
def mpesa_webhook(token: str):
    expected = str(current_app.config.get("MPESA_CALLBACK_TOKEN", ""))
    if not expected or not hmac.compare_digest(token, expected):
        abort(404)
    allowlist = current_app.config.get("MPESA_CALLBACK_IP_ALLOWLIST", ())
    if allowlist:
        remote = request.remote_addr or ""
        try:
            address = ipaddress.ip_address(remote)
            if not any(address in ipaddress.ip_network(net) for net in allowlist):
                abort(403)
        except ValueError:
            abort(403)
    return _handle_webhook("mpesa", allow_tokenized_mpesa=True)


def _handle_webhook(provider: str, *, allow_tokenized_mpesa: bool = False):
    if provider.lower() not in {"mock", "stripe"} and not (
        provider.lower() == "mpesa" and allow_tokenized_mpesa
    ):
        abort(404)
    if provider.lower() == "mock" and current_app.config.get("AGRI_LINK_CONFIG") == "production":
        abort(404)
    try:
        PaymentService.process_webhook(provider, request.get_data(cache=False), request.headers)
    except InvalidProviderEvent:
        abort(400)
    except PaymentProviderError:
        abort(503)
    return "", 204
