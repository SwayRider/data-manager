from flask import Flask, jsonify, render_template, request

from datamanager.blueprints import register_blueprints
from datamanager.cli import register_cli
from datamanager.config import config as app_config
from datamanager.db import SessionLocal
from datamanager.logging_setup import configure_logging


def _register_error_handlers(app: Flask) -> None:
    def handle(error):
        code = getattr(error, "code", 500)
        if request.path.startswith("/health") or "/api/" in request.path:
            return jsonify(error=getattr(error, "name", "Internal Server Error")), code
        return render_template("errors/error.html", code=code, error=error), code

    app.register_error_handler(404, handle)
    app.register_error_handler(500, handle)


def create_app() -> Flask:
    configure_logging(level=app_config.LOG_LEVEL, json_output=app_config.LOG_JSON)
    app_config.ensure_data_dirs()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = app_config.SECRET_KEY

    register_blueprints(app)
    _register_error_handlers(app)
    register_cli(app)

    @app.teardown_appcontext
    def remove_session(exception=None):
        SessionLocal.remove()

    return app
