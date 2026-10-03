"""HTTP error pages."""

from flask import Flask, render_template


def register_error_handlers(app: Flask) -> None:
    for status_code in (400, 403, 404, 413, 429, 500):
        app.register_error_handler(status_code, _render_error(status_code))


def _render_error(status_code: int):
    def handler(error):
        if status_code == 500:
            app_logger = __import__("flask").current_app.logger
            app_logger.exception("Unhandled application error", exc_info=error)
        return render_template("error.html", status_code=status_code), status_code

    return handler
