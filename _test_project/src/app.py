"""Flask application entry point."""
import logging
from flask import Flask, jsonify
from src.user_router import user_bp
from src.middleware import handle_errors
from config import DEBUG, LOG_LEVEL

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.register_blueprint(user_bp, url_prefix="/api/users")


@app.route("/health")
@handle_errors
def health():
    return {"status": "ok"}


@app.route("/api/info")
@handle_errors
def info():
    logger.debug("Info endpoint hit, debug=%s", DEBUG)
    return jsonify({"version": "1.0.0", "debug": DEBUG})


if __name__ == "__main__":
    logger.debug("Starting server on port 5000, log_level=%s", LOG_LEVEL)
    app.run(debug=DEBUG, port=5000)
