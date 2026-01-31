"""
Flask application factory for Attention Flow Desk.
"""

from pathlib import Path

from flask import Flask


def create_app() -> Flask:
    """Create and configure the Flask application."""
    # Get project root (attention-desk directory)
    base_dir = Path(__file__).parent.parent.parent

    app = Flask(
        __name__,
        static_folder=str(base_dir / "ui"),
        static_url_path="/static"
    )

    # Register API routes
    from .routes import api
    app.register_blueprint(api)

    # Serve the main UI at root
    @app.route("/")
    def index():
        return app.send_static_file("mockup.html")

    return app
