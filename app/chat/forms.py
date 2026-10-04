"""CSRF-protected chat and offer forms."""

from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField
from wtforms.validators import InputRequired, Length


class MessageForm(FlaskForm):
    body = TextAreaField("Message", validators=[InputRequired(), Length(max=2000)])
    submit = SubmitField("Send")


class OfferForm(FlaskForm):
    quantity = StringField("Quantity", validators=[InputRequired(), Length(max=10)])
    unit_price = StringField("KES per unit", validators=[InputRequired(), Length(max=18)])
    submit = SubmitField("Make offer")


class OfferActionForm(FlaskForm):
    submit = SubmitField()
