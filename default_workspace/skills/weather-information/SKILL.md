---
name: weather-information
description: Retrieve and summarize current weather and the day's forecast for a location. Use when the user asks about weather in a city, region, or coordinates; wants a forecast; or asks whether conditions are suitable for travel, events, clothing, or outdoor activities.
when_to_use:
  - The user asks about current weather, today's weather, or a forecast for a location
  - The user asks whether conditions suit travel, events, clothing, or outdoor activity
required_tools:
  - skill_run_script
scripts:
  - path: scripts/get_weather.py
    description: Fetch current conditions and the day's hourly forecast for a location from wttr.in.
    when_to_run: Run once whenever the user asks about weather; pass the location name (defaults to Brisbane).
---

# Weather Information

Get weather fast from one source and answer in a short, friendly summary.

## Workflow

1. Pick the location. Default to **Brisbane** when the user does not name one.
2. Run the one declared script with the `skill_run_script` tool — nothing else:

   - `skill_name`: `weather-information`
   - `script`: `scripts/get_weather.py`
   - `args`: `["Brisbane"]` (replace with the requested location)

   Do not use bash, web search, or a research/subagent dispatch. One call is enough.
3. Write the answer directly from that output, in the user's language.

The script returns **today plus the next two days** — a `current` line, then one
`day <date> ...` block per day with hourly slots. This is the single source for
both "today" and multi-day forecast questions. For a forecast, summarize the
relevant `day` blocks. Never fall back to web search or another agent to extend
the forecast; if the user asks beyond what the script returns (more than 3 days),
say only up to 3 days are available.

## Output Format

Answer in plain Markdown (no code block, no language label). The template below is in
English for reference only — write the actual answer in the user's language. Match this shape:

```
Today in Brisbane it's sunny and dry ☀️

- Right now about <b>+18°C</b>, sunny
- Daytime high around <b>+20°C</b>
- Cool this morning, low around <b>+10°C</b>
- 🌧️ No rain expected — chance <b>0–3%</b>
- 🌬️ Light wind: about <b>5–10 km/h</b>, south/southwest
- Clear in the evening, around <b>+14…15°C</b>

A calm, pleasant day for a walk, but chilly morning and evening — bring a light jacket.
```

Rules:
- Write the response in the user's language; the example above is only a structural guide.
- One opening line: location + overall mood, with a fitting emoji.
- A blank line, then bullets — one fact each, each on its own line: now, daytime high, morning low, rain chance, wind, evening.
- Bold the temperatures and key numbers.
- Close with one short, warm practical tip (clothing or activity).
- Keep it brief. Skip details the user did not ask about.
- Never invent values — use only the script output.

## Notes

- Temperatures are °C, wind is km/h.
- If the script fails (network/location), say so plainly and ask the user to retry or give a clearer location. Do not guess values.
