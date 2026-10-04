"""CSRF-protected administrative action forms."""

from flask_wtf import FlaskForm
from wtforms import SubmitField


class ConfirmActionForm(FlaskForm):
    submit = SubmitField("Confirm")
