"""Authentication request validation."""

from flask_wtf import FlaskForm
from wtforms import PasswordField, RadioField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError

from app.utils.phone import normalize_phone


class RegisterForm(FlaskForm):
    phone = StringField("Phone number", validators=[DataRequired(), Length(max=40)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10, max=256)])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired()])
    submit = SubmitField("Create account")

    def validate_phone(self, field):
        try:
            field.data = normalize_phone(field.data)
        except ValueError as exc:
            raise ValidationError("Enter a valid phone number, such as +254712345678.") from exc

    def validate_confirm_password(self, field):
        if field.data != self.password.data:
            raise ValidationError("Passwords do not match.")


class VerifyOtpForm(FlaskForm):
    code = StringField("6-digit code", validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField("Verify phone")

    def validate_code(self, field):
        if not field.data.isascii() or not field.data.isdigit():
            raise ValidationError("Enter the 6-digit code.")


class LoginForm(FlaskForm):
    phone = StringField("Phone number", validators=[DataRequired(), Length(max=40)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=256)])
    submit = SubmitField("Log in")

    def validate_phone(self, field):
        try:
            field.data = normalize_phone(field.data)
        except ValueError as exc:
            raise ValidationError("Enter a valid phone number and password.") from exc


class ChooseRoleForm(FlaskForm):
    role = RadioField(
        "I am joining as a",
        choices=[("FARMER", "Farmer"), ("BUYER", "Buyer")],
        validators=[DataRequired()],
    )
    submit = SubmitField("Continue")


class ResendOtpForm(FlaskForm):
    submit = SubmitField("Resend code")
