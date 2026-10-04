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

    @app.cli.group("payments")
    def payments_group() -> None:
        """Development and payment operations."""

    @payments_group.command("mock-event")
    @click.argument("payment_id", type=int)
    @click.argument(
        "outcome",
        type=click.Choice(
            ["pending", "success", "failed", "expired", "refunded"], case_sensitive=False
        ),
    )
    def mock_payment_event(payment_id: int, outcome: str) -> None:
        """Simulate a signed mock provider event outside production."""
        from app.payments.services import PaymentService

        try:
            event = PaymentService.simulate_mock_event(payment_id, outcome)
        except Exception as exc:
            # Keep provider and database internals out of CLI output.
            raise click.ClickException(
                str(exc) if isinstance(exc, ValueError) else "Mock event could not be processed."
            ) from exc
        click.echo(
            f"Mock event {event.provider_event_id}: {event.processing_status.lower()} "
            f"({event.result_message})."
        )

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
