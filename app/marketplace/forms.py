"""HTML form validation for marketplace operations."""

import re
from decimal import Decimal, InvalidOperation

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, MultipleFileField
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from app.marketplace.constants import KENYAN_COUNTIES
from app.models.marketplace import QualityGrade, Unit


class ListingForm(FlaskForm):
    category_id = SelectField("Category", coerce=int, validators=[DataRequired()])
    title = StringField("Listing title", validators=[DataRequired(), Length(min=3, max=120)])
    description = TextAreaField("Description", validators=[DataRequired(), Length(max=5000)])
    quantity = StringField("Quantity", validators=[DataRequired(), Length(max=12)])
    unit = SelectField("Unit", choices=[(v.value, v.value.title()) for v in Unit])
    grade = SelectField("Grade", choices=[(v.value, v.value.title()) for v in QualityGrade])
    price = StringField("Price (KES)", validators=[DataRequired(), Length(max=18)])
    county = SelectField(
        "County", choices=[("", "Choose county"), *((v, v) for v in KENYAN_COUNTIES)]
    )
    location_name = StringField("Area or market", validators=[Optional(), Length(max=120)])
    latitude = StringField("Latitude", validators=[Optional(), Length(max=32)])
    longitude = StringField("Longitude", validators=[Optional(), Length(max=32)])
    images = MultipleFileField("Photos", validators=[FileAllowed(["jpg", "jpeg", "png", "webp"])])
    submit = SubmitField("Save listing")

    def __init__(self, *args, categories=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.category_id.choices = [(c.id, c.name) for c in categories]

    def validate_quantity(self, field):
        if not field.data or not field.data.isascii() or not field.data.isdecimal():
            raise ValidationError("Enter a whole-number quantity greater than zero.")
        if int(field.data) <= 0 or int(field.data) > 2_147_483_647:
            raise ValidationError("Quantity must be greater than zero.")

    def validate_price(self, field):
        try:
            raw = field.data.strip()
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", raw):
                raise InvalidOperation
            value = Decimal(raw)
            if value <= 0 or value * 100 > 2_147_483_647:
                raise InvalidOperation
        except (InvalidOperation, AttributeError) as exc:
            raise ValidationError(
                "Enter a valid price in KES greater than zero, up to 2 decimals."
            ) from exc

    def validate_latitude(self, field):
        _validate_coordinate(field.data, -90, 90, "Latitude")

    def validate_longitude(self, field):
        _validate_coordinate(field.data, -180, 180, "Longitude")


def _validate_coordinate(raw, minimum, maximum, label):
    if raw in (None, ""):
        return
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise ValidationError(f"Enter a valid {label.lower()}.") from exc
    if not value.is_finite() or value < minimum or value > maximum:
        raise ValidationError(f"Enter a valid {label.lower()}.")


class ListingSearchForm(FlaskForm):
    q = StringField("Search", validators=[Optional(), Length(max=120)])
    category = SelectField("Category", validators=[Optional()], choices=[])
    county = SelectField("County", validators=[Optional()], choices=[])
    grade = SelectField("Grade", validators=[Optional()], choices=[])
    unit = SelectField("Unit", validators=[Optional()], choices=[])
    min_price = StringField("Min price (KES)", validators=[Optional(), Length(max=18)])
    max_price = StringField("Max price (KES)", validators=[Optional(), Length(max=18)])
    sort = SelectField(
        "Sort",
        choices=[
            ("newest", "Newest"),
            ("oldest", "Oldest"),
            ("price_low", "Price: low to high"),
            ("price_high", "Price: high to low"),
        ],
    )
    submit = SubmitField("Search")
