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
        """Seed the platform-controlled marketplace categories."""
        from app.marketplace.services import seed_categories

        count = seed_categories()
        click.echo(f"Marketplace categories ready ({count} added).")

    @app.cli.group("jobs")
    def jobs_group() -> None:
        """Maintenance job commands."""

    @jobs_group.command("run-maintenance")
    def maintenance_command() -> None:
        """Expire offers whose response window has elapsed."""
        from app.chat.services import expire_pending_offers

        click.echo(f"Expired {expire_pending_offers()} pending offer(s).")

    @jobs_group.command("expire-offers")
    def expire_offers_command() -> None:
        """Expire pending offers after their 48-hour response window."""
        from app.chat.services import expire_pending_offers

        click.echo(f"Expired {expire_pending_offers()} pending offer(s).")
