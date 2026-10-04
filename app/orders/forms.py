"""Order and private delivery input forms."""

from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import InputRequired, Length, Optional

from app.marketplace.constants import KENYAN_COUNTIES


class DeliveryForm(FlaskForm):
    method = SelectField(
        "Fulfillment method",
        choices=[("PICKUP", "Pickup"), ("DELIVERY", "Delivery")],
        validators=[InputRequired()],
    )
    destination_county = SelectField(
        "County",
        choices=[("", "Choose a county")] + [(item, item) for item in KENYAN_COUNTIES],
        validators=[Optional()],
    )
    location_name = StringField("Area or town", validators=[Optional(), Length(max=120)])
    address_text = TextAreaField("Delivery directions", validators=[Optional(), Length(max=500)])
    recipient_name = StringField("Recipient", validators=[Optional(), Length(max=120)])
    recipient_phone = StringField("Recipient phone", validators=[Optional(), Length(max=40)])
    submit = SubmitField("Save fulfillment details")


class OrderActionForm(FlaskForm):
    submit = SubmitField()
