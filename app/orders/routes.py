"""Buyer and seller order pages and CSRF-protected service actions."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import Conflict, NotFound

from app.disputes.forms import DisputeForm
from app.disputes.services import DisputeService
from app.extensions import socketio
from app.orders.forms import DeliveryForm, OrderActionForm
from app.orders.services import OrderService, OrderValidationError
from app.payments.services import PaymentService
from app.reviews.forms import ReviewForm
from app.reviews.services import ReviewService

orders_bp = Blueprint("orders", __name__)


@orders_bp.get("/my/orders")
@login_required
def buyer_orders():
    if current_user.role != "BUYER" or current_user.phone_verified_at is None:
        abort(404)
    return _order_list("buyer")


@orders_bp.get("/my/sales")
@login_required
def seller_orders():
    if current_user.role != "FARMER" or current_user.phone_verified_at is None:
        abort(404)
    return _order_list("seller")


def _order_list(view):
    page = _page_arg(request.args.get("page"))
    status = request.args.get("status", "")
    pagination = OrderService.list_orders(current_user, page=page, status=status)
    return render_template("orders/list.html", pagination=pagination, view=view, status=status)


@orders_bp.get("/orders/<int:order_id>")
@login_required
def detail(order_id: int):
    try:
        order = OrderService.get_order(current_user, order_id)
    except NotFound:
        abort(404)
    delivery_form = DeliveryForm()
    payment = PaymentService.latest_for_order(current_user, order.id)
    review = ReviewService.get_order_review(current_user, order.id)
    dispute = DisputeService.get_for_order(current_user, order.id)
    return render_template(
        "orders/detail.html",
        order=order,
        form=delivery_form,
        payment=payment,
        review=review,
        review_form=ReviewForm(),
        dispute=dispute,
        dispute_form=DisputeForm(),
    )


@orders_bp.post("/orders/<int:order_id>/delivery")
@login_required
def save_delivery(order_id: int):
    form = DeliveryForm()
    if not form.validate_on_submit():
        flash("Check the fulfillment details and try again.", "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    try:
        delivery = OrderService.save_delivery(current_user, order_id, form.data)
        flash(f"Fulfillment method saved: {delivery.method.lower()}.", "success")
    except NotFound:
        abort(404)
    except (OrderValidationError, Conflict) as exc:
        flash(str(exc), "warning")
    return redirect(url_for("orders.detail", order_id=order_id))


@orders_bp.post("/orders/<int:order_id>/actions/<action>")
@login_required
def action(order_id: int, action: str):
    if not OrderActionForm().validate_on_submit():
        abort(400)
    try:
        order = OrderService.transition(current_user, order_id, action)
    except NotFound:
        abort(404)
    except (OrderValidationError, Conflict) as exc:
        flash(str(exc), "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    socketio.emit(
        "order:updated",
        {"order_id": order.id, "status": order.status},
        to=f"user:{order.buyer_id}",
    )
    socketio.emit(
        "order:updated",
        {"order_id": order.id, "status": order.status},
        to=f"user:{order.seller_id}",
    )
    flash(f"Order updated to {order.status.replace('_', ' ').lower()}.", "success")
    return redirect(url_for("orders.detail", order_id=order_id))


def _page_arg(value) -> int:
    try:
        return max(1, min(int(value or 1), 100_000))
    except TypeError, ValueError:
        return 1
