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
        """Expire offers and complete delivered orders past the grace period."""
        from app.chat.services import expire_pending_offers
        from app.orders.services import OrderService

        expired = expire_pending_offers()
        completed = OrderService.complete_delivered_orders()
        click.echo(f"Expired {expired} offer(s); completed {completed} delivered order(s).")

    @jobs_group.command("expire-offers")
    def expire_offers_command() -> None:
        """Expire pending offers after their 48-hour response window."""
        from app.chat.services import expire_pending_offers

        click.echo(f"Expired {expire_pending_offers()} pending offer(s).")

    @jobs_group.command("complete-orders")
    def complete_orders_command() -> None:
        """Complete delivered orders after the 72-hour buyer review window."""
        from app.orders.services import OrderService

        click.echo(f"Completed {OrderService.complete_delivered_orders()} order(s).")
