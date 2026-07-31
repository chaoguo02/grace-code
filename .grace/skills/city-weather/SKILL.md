---
name: city-weather
description: Query deterministic city weather through the weather-mock MCP server and summarize current conditions, forecasts, comparisons, and travel advice. Use when the user asks for weather in one or more cities, a multi-day city forecast, a city-weather comparison, or an end-to-end Skill-to-MCP workflow test.
evidence:
  required-tool-calls:
    - tool: mcp:weather_mock:weather_get_current
      foreach-argument: city
---

# City Weather

Use the weather MCP as the only source of weather facts. This skill coordinates
tools; it does not contain weather data itself.

## Workflow

1. Extract every requested city and determine whether the user asked for
   current conditions, a forecast, or both. Preserve explicit output limits
   such as an exact number of lines.
2. For each city, call
   `mcp__weather_mock__weather_get_current` with `{"city": "<city>"}`.
3. Only when the user asks for future weather, a forecast, a trend, or a
   number of days, call `mcp__weather_mock__weather_get_forecast` with
   `{"city": "<city>", "days": <days>}`. Default to 2 days and accept 1-5.
4. Check that every result identifies the same normalized city and includes
   `fixture: grace-code-weather-mock-v1`.
5. Summarize only the requested facts. Add advice or a city comparison only
   when requested or when it fits without violating an explicit format limit.

Independent calls for different cities may run in parallel. Do not create a
subagent merely because this skill was invoked.

## Integrity Rules

- Never substitute WebSearch, prior knowledge, or invented weather when an MCP
  call is missing or fails.
- State clearly that results are deterministic mock data intended for MCP/Skill
  integration testing.
- If a city is unsupported, report the MCP error and its supported-city list.
- If a required MCP tool is unavailable, stop and name the missing tool.
- Do not expose hidden reasoning or repeat raw tool payloads unnecessarily.

## Output

When the user does not specify a format, use:

```text
城市：<normalized city>
当前：<condition and temperature>
未来：<one line per forecast day, only when requested>
建议：<brief advice, only when requested>
数据：weather-mock fixture
```

For multiple cities, give each city the same compact block followed by a
comparison when requested. Explicit user constraints such as "three lines",
"JSON", or "current weather only" override this default template. Produce one
final answer after all calls complete.
