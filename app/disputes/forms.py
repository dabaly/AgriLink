"""Plain-text buyer dispute and administrator resolution forms."""

from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField, TextAreaField
from wtforms.validators import InputRequired, Length

from app.models.feedback import DisputeReason


class DisputeForm(FlaskForm):
    reason = SelectField(
        "Reason",
        choices=[
            (reason.value, reason.value.replace("_", " ").title()) for reason in DisputeReason
        ],
        validators=[InputRequired()],
    )
    description = TextAreaField("What happened?", validators=[InputRequired(), Length(max=4000)])
    submit = SubmitField("Open dispute")


class DisputeResolutionForm(FlaskForm):
    note = TextAreaField("Resolution note", validators=[InputRequired(), Length(max=2000)])
    submit = SubmitField("Continue")
