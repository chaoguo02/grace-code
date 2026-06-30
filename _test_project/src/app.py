"""Flask application entry point."""
from flask import Flask, jsonify
from src.user_router import user_bp
from src.auth import require_auth
from src.middleware import handle_errors
from config import DEBUG, LOG_LEVEL

app = Flask(__name__)
app.register_blueprint(user_bp, url_prefix="/api/users")


@app.route("/health")
@handle_errors
def health():
    return {"status": "ok"}


@app.route("/api/info")
@handle_errors
def info():
    print(f"Info endpoint hit, debug={DEBUG}")
    return jsonify({"version": "1.0.0", "debug": DEBUG})


if __name__ == "__main__":
    print(f"Starting server on port 5000, log_level={LOG_LEVEL}")
    app.run(debug=DEBUG, port=5000)
