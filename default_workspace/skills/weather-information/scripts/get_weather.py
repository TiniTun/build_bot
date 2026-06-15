#!/usr/bin/env python3
"""Fetch weather for a location from wttr.in and print compact structured data.

Usage: python3 get_weather.py "Brisbane"
Defaults to Brisbane when no location is given.

wttr.in returns today plus the next two days, so this prints all available days
(current conditions followed by a daily summary and hourly slots per day). That
makes the output sufficient for both "today" and multi-day forecast questions.
"""
import json
import sys
import urllib.parse
import urllib.request

location = sys.argv[1] if len(sys.argv) > 1 else "Brisbane"
url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"

data = json.load(urllib.request.urlopen(url, timeout=15))
cur = data["current_condition"][0]

print(
    "current",
    cur["temp_C"],
    cur["FeelsLikeC"],
    cur["weatherDesc"][0]["value"],
    cur["humidity"],
    cur["windspeedKmph"],
    cur["winddir16Point"],
    cur.get("uvIndex"),
    cur.get("precipMM"),
    cur.get("visibility"),
)

for w in data["weather"]:
    print(
        "day",
        w["date"],
        w.get("mintempC"),
        w.get("maxtempC"),
        w.get("avgtempC"),
        w.get("uvIndex"),
    )
    for h in w["hourly"]:
        if h["time"] in ["0", "300", "600", "900", "1200", "1500", "1800", "2100"]:
            print(
                h["time"].rjust(4, "0"),
                h["tempC"],
                h["FeelsLikeC"],
                h["weatherDesc"][0]["value"].strip(),
                "rain%",
                h["chanceofrain"],
                "precip",
                h["precipMM"],
                "wind",
                h["windspeedKmph"],
                h["winddir16Point"],
                "gust",
                h.get("WindGustKmph"),
                "hum",
                h["humidity"],
                "cloud",
                h["cloudcover"],
            )
