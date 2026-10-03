"""Project command registration."""

import click
from flask import Flask


def register_cli(app: Flask) -> None:
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
