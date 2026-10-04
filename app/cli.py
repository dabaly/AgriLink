"""Project command registration."""

import click
from flask import Flask


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    def create_admin_command() -> None:
        """Create a verified administrator from a trusted terminal session."""
        from app.auth.services import AuthenticationError, create_admin_user

        phone = click.prompt("Admin phone number")
        password = click.prompt("Admin password", hide_input=True, confirmation_prompt=True)
        try:
            user = create_admin_user(phone, password)
        except (AuthenticationError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Created admin account {user.phone}.")

    @app.cli.command("seed")
    def seed_command() -> None:
        """Seed development data (currently no domain data is defined)."""
        click.echo("No seed data is defined yet.")

    @app.cli.group("jobs")
    def jobs_group() -> None:
        """Maintenance job commands."""

    @jobs_group.command("run-maintenance")
    def maintenance_command() -> None:
        """Run scheduled maintenance tasks (none are defined yet)."""
        click.echo("No maintenance tasks are defined yet.")
