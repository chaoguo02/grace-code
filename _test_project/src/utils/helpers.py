"""General helper functions."""
import json
from datetime import datetime


def format_response(data, status="success"):
    """Wrap data in a standard API response format."""
    return {
        "status": status,
        "data": data,
        "timestamp": datetime.now().isoformat(),
    }


def parse_pagination(args):
    """Extract pagination params from request args."""
    try:
        page = int(args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(args.get("per_page", 20))
    except (ValueError, TypeError):
        per_page = 20
    page = max(1, page)
    per_page = min(per_page, 100)
    return page, per_page


def safe_json_loads(text: str, default=None):
    """Parse JSON string safely, return default on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default
