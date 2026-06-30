"""Flask application entry point."""
import logging
from flask import Flask, jsonify
from src.user_router import user_bp
from src.auth import require_auth
from config import DEBUG, LOG_LEVEL

app = Flask(__name__)
logging.basicConfig(level=LOG_LEVEL)
app.register_blueprint(user_bp, url_prefix="/api/users")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/api/info")
def info():
    logging.info("Info endpoint hit, debug=%s", DEBUG)
    return jsonify({"version": "1.0.0", "debug": DEBUG})


if __name__ == "__main__":
    logging.info("Starting server on port 5000, log_level=%s", LOG_LEVEL)
    app.run(debug=DEBUG, port=5000)
