"""Participant dispute pages and minimal administrator resolution pages."""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import Conflict, NotFound

from app.disputes import disputes_bp
from app.disputes.forms import DisputeForm, DisputeResolutionForm
from app.disputes.services import DisputeService, DisputeValidationError


@disputes_bp.post("/orders/<int:order_id>/disputes")
@login_required
def open_dispute(order_id: int):
    form = DisputeForm()
    if not form.validate_on_submit():
        flash("Choose a reason and enter a plain-text description.", "warning")
        return redirect(url_for("orders.detail", order_id=order_id))
    try:
        DisputeService.create_dispute(
            current_user, order_id, form.reason.data, form.description.data
        )
    except NotFound:
        abort(404)
    except (Conflict, DisputeValidationError) as exc:
        flash(str(exc), "warning")
    else:
        flash("Your dispute was opened for administrator review.", "success")
    return redirect(url_for("orders.detail", order_id=order_id))


@disputes_bp.get("/my/disputes/<int:dispute_id>")
@login_required
def participant_detail(dispute_id: int):
    try:
        dispute = DisputeService.get_for_participant(current_user, dispute_id)
    except NotFound:
        abort(404)
    return render_template("disputes/participant_detail.html", dispute=dispute)


@disputes_bp.get("/admin/disputes")
@login_required
def admin_list():
    try:
        pagination = DisputeService.list_for_admin(
            current_user, page=request.args.get("page", 1, type=int)
        )
    except NotFound:
        abort(404)
    return render_template("disputes/admin_list.html", pagination=pagination)


@disputes_bp.get("/admin/disputes/<int:dispute_id>")
@login_required
def admin_detail(dispute_id: int):
    try:
        dispute = DisputeService.get_for_admin(current_user, dispute_id)
    except NotFound:
        abort(404)
    return render_template(
        "disputes/admin_detail.html", dispute=dispute, form=DisputeResolutionForm()
    )


@disputes_bp.post("/admin/disputes/<int:dispute_id>/actions/<action>")
@login_required
def admin_action(dispute_id: int, action: str):
    form = DisputeResolutionForm()
    if action in {"resolve_for_buyer", "resolve_for_seller"} and not form.validate_on_submit():
        flash("A plain-text resolution note is required.", "warning")
        return redirect(url_for("disputes.admin_detail", dispute_id=dispute_id))
    note = form.note.data if form.validate_on_submit() else ""
    try:
        dispute = DisputeService.admin_transition(current_user, dispute_id, action, note)
    except NotFound:
        abort(404)
    except (Conflict, DisputeValidationError) as exc:
        flash(str(exc), "warning")
    else:
        if dispute.resolution_pending:
            flash(
                "Refund requested. The dispute remains under review until provider confirmation.",
                "info",
            )
        else:
            flash("Dispute updated.", "success")
    return redirect(url_for("disputes.admin_detail", dispute_id=dispute_id))
