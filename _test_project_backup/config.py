"""Application configuration."""
import os

DEBUG = os.getenv("FLASK_DEBUG", "1") == "1"
SECRET_KEY = os.getenv("SECRET_KEY", "my-super-secret-key-change-in-production")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///app.db")
TOKEN_EXPIRY_HOURS = int(os.getenv("TOKEN_EXPIRY_HOURS", "24"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if DEBUG else "INFO")
