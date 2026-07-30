"""Deterministic weather MCP server for end-to-end integration tests.

The fixture deliberately performs no network access.  It exposes stable data so
tool discovery, Skill invocation, child-session routing, rendering, and replay
can be tested without a third-party weather service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


FIXTURE_ID = "grace-code-weather-mock-v1"
OBSERVED_AT = "2026-07-30T09:00:00+08:00"

_WEATHER: dict[str, dict[str, Any]] = {
    "北京": {
        "aliases": ("北京", "beijing"),
        "current": {
            "condition": "晴",
            "temperature_c": 28,
            "humidity_percent": 42,
            "wind": "东北风 2级",
        },
        "forecast": (
            {"date": "2026-07-30", "condition": "晴", "high_c": 28, "low_c": 18},
            {"date": "2026-07-31", "condition": "多云", "high_c": 26, "low_c": 17},
            {"date": "2026-08-01", "condition": "晴", "high_c": 29, "low_c": 19},
            {"date": "2026-08-02", "condition": "阵雨", "high_c": 25, "low_c": 18},
            {"date": "2026-08-03", "condition": "多云", "high_c": 27, "low_c": 18},
        ),
    },
    "上海": {
        "aliases": ("上海", "shanghai"),
        "current": {
            "condition": "小雨",
            "temperature_c": 25,
            "humidity_percent": 81,
            "wind": "东南风 3级",
        },
        "forecast": (
            {"date": "2026-07-30", "condition": "小雨", "high_c": 25, "low_c": 21},
            {"date": "2026-07-31", "condition": "阴", "high_c": 27, "low_c": 22},
            {"date": "2026-08-01", "condition": "多云", "high_c": 29, "low_c": 23},
            {"date": "2026-08-02", "condition": "中雨", "high_c": 26, "low_c": 22},
            {"date": "2026-08-03", "condition": "阴", "high_c": 28, "low_c": 22},
        ),
    },
    "深圳": {
        "aliases": ("深圳", "shenzhen"),
        "current": {
            "condition": "雷阵雨",
            "temperature_c": 31,
            "humidity_percent": 86,
            "wind": "南风 3级",
        },
        "forecast": (
            {"date": "2026-07-30", "condition": "雷阵雨", "high_c": 31, "low_c": 26},
            {"date": "2026-07-31", "condition": "多云", "high_c": 32, "low_c": 27},
            {"date": "2026-08-01", "condition": "阵雨", "high_c": 31, "low_c": 26},
            {"date": "2026-08-02", "condition": "多云", "high_c": 33, "low_c": 27},
            {"date": "2026-08-03", "condition": "雷阵雨", "high_c": 30, "low_c": 26},
        ),
    },
}

_CITY_LOOKUP = {
    alias.casefold(): city
    for city, record in _WEATHER.items()
    for alias in record["aliases"]
}

CURRENT_TOOL = Tool(
    name="weather_get_current",
    description=(
        "Return deterministic mock current weather for Beijing, Shanghai, or "
        "Shenzhen. Intended for Grace Code MCP integration tests; no network."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name in Chinese or English.",
            },
        },
        "required": ["city"],
        "additionalProperties": False,
    },
)

FORECAST_TOOL = Tool(
    name="weather_get_forecast",
    description=(
        "Return 1-5 days of deterministic mock forecast data for Beijing, "
        "Shanghai, or Shenzhen. Intended for Grace Code MCP integration tests."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name in Chinese or English.",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 2,
                "description": "Number of forecast days.",
            },
        },
        "required": ["city"],
        "additionalProperties": False,
    },
)

server = Server("grace-code-weather-mock")


def _normalize_city(value: Any) -> str:
    city = str(value or "").strip()
    normalized = _CITY_LOOKUP.get(city.casefold())
    if normalized is None:
        supported = ", ".join(_WEATHER)
        raise ValueError(
            f"Unsupported city {city!r}. Supported cities: {supported}"
        )
    return normalized


def _json_content(payload: dict[str, Any]) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    ]


def current_weather(arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a current-weather response without transport dependencies."""
    city = _normalize_city(arguments.get("city"))
    return {
        "fixture": FIXTURE_ID,
        "observed_at": OBSERVED_AT,
        "city": city,
        **_WEATHER[city]["current"],
    }


def weather_forecast(arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a forecast response without transport dependencies."""
    city = _normalize_city(arguments.get("city"))
    raw_days = arguments.get("days", 2)
    if isinstance(raw_days, bool):
        raise ValueError("days must be an integer between 1 and 5")
    try:
        days = int(raw_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be an integer between 1 and 5") from exc
    if days < 1 or days > 5:
        raise ValueError("days must be an integer between 1 and 5")
    return {
        "fixture": FIXTURE_ID,
        "generated_at": OBSERVED_AT,
        "city": city,
        "days": days,
        "forecast": list(_WEATHER[city]["forecast"][:days]),
    }


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [CURRENT_TOOL, FORECAST_TOOL]


@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[TextContent]:
    if name == CURRENT_TOOL.name:
        return _json_content(current_weather(arguments))
    if name == FORECAST_TOOL.name:
        return _json_content(weather_forecast(arguments))
    raise ValueError(f"Unknown weather tool: {name}")


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
