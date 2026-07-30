---
name: city-weather
description: Query deterministic city weather through the weather-mock MCP server and summarize current conditions, forecasts, comparisons, and travel advice. Use when the user asks for weather in one or more cities, a multi-day city forecast, a city-weather comparison, or an end-to-end Skill-to-MCP workflow test.
---

# City Weather

Use the weather MCP as the only source of weather facts. This skill coordinates
tools; it does not contain weather data itself.

## Workflow

1. Extract every requested city and the forecast length. Default to 2 days and
   accept only 1-5 days.
2. For each city, call
   `mcp__weather_mock__weather_get_current` with `{"city": "<city>"}`.
3. For each city, call
   `mcp__weather_mock__weather_get_forecast` with
   `{"city": "<city>", "days": <days>}`.
4. Check that both results identify the same normalized city and include
   `fixture: grace-code-weather-mock-v1`.
5. Summarize the returned conditions and give brief advice based only on those
   conditions. For multiple cities, add a direct comparison.

Independent calls for different cities may run in parallel. Do not create a
subagent merely because this skill was invoked.

## Integrity Rules

- Never substitute WebSearch, prior knowledge, or invented weather when an MCP
  call is missing or fails.
- State clearly that results are deterministic mock data intended for MCP/Skill
  integration testing.
- If a city is unsupported, report the MCP error and its supported-city list.
- If either required MCP tool is unavailable, stop and name the missing tool.
- Do not expose hidden reasoning or repeat raw tool payloads unnecessarily.

## Output

For one city, return:

```text
城市：<normalized city>
当前：<condition and temperature>
未来：<one line per forecast day>
建议：<brief advice grounded in the returned data>
数据：weather-mock fixture
```

For multiple cities, give each city the same compact block followed by a
comparison. Produce one final answer after all calls complete.
