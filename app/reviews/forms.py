"""Plain-text transaction review forms."""

from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField, TextAreaField
from wtforms.validators import InputRequired, Length


class ReviewForm(FlaskForm):
    rating = SelectField(
        "Rating",
        coerce=int,
        choices=[
            (5, "5 · Excellent"),
            (4, "4 · Good"),
            (3, "3 · Fair"),
            (2, "2 · Poor"),
            (1, "1 · Very poor"),
        ],
        validators=[InputRequired()],
    )
    body = TextAreaField("Your review", validators=[InputRequired(), Length(max=2000)])
    submit = SubmitField("Submit review")
