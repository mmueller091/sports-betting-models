"""
Weather fetcher using Open-Meteo (free, no API key required).
Fetches game-time temperature, wind speed, and wind direction.
"""

import httpx
from typing import Optional

# Ballpark coordinates (lat, lon)
PARK_COORDS = {
    "Yankee Stadium": (40.8296, -73.9262),
    "Fenway Park": (42.3467, -71.0972),
    "Wrigley Field": (41.9484, -87.6553),
    "Oracle Park": (37.7786, -122.3893),
    "Dodger Stadium": (34.0739, -118.2400),
    "Coors Field": (39.7559, -104.9942),
    "Petco Park": (32.7076, -117.1570),
    "T-Mobile Park": (47.5914, -122.3325),
    "Globe Life Field": (32.7473, -97.0836),
    "American Family Field": (43.0280, -87.9712),
    "Great American Ball Park": (39.0979, -84.5082),
    "Camden Yards": (39.2838, -76.6217),
    "Truist Park": (33.8907, -84.4677),
    "loanDepot park": (25.7781, -80.2197),
    "Nationals Park": (38.8730, -77.0074),
    "Citizens Bank Park": (39.9061, -75.1665),
    "Citi Field": (40.7571, -73.8458),
    "PNC Park": (40.4469, -80.0057),
    "Busch Stadium": (38.6226, -90.1928),
    "Chase Field": (33.4453, -112.0667),
    "Minute Maid Park": (29.7572, -95.3555),
    "Angel Stadium": (33.8003, -117.8827),
    "Oakland Coliseum": (37.7516, -122.2005),
    "Kauffman Stadium": (39.0514, -94.4803),
    "Target Field": (44.9817, -93.2781),
    "Guaranteed Rate Field": (41.8300, -87.6339),
    "Progressive Field": (41.4954, -81.6853),
    "Comerica Park": (42.3390, -83.0485),
    "Rogers Centre": (43.6414, -79.3894),
    "Tropicana Field": (27.7682, -82.6534),
    "default": (40.0, -75.0),
}

# Retractable/domed stadiums where weather matters less
INDOOR_PARKS = {
    "Chase Field",
    "loanDepot park",
    "Tropicana Field",
    "Rogers Centre",
    "Minute Maid Park",
    "Globe Life Field",
    "American Family Field",
}

WIND_DIR_LABELS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
]


def _wind_direction_label(degrees: float) -> str:
    idx = round(degrees / 22.5) % 16
    return WIND_DIR_LABELS[idx]


async def get_game_weather(venue: str, game_time_utc: Optional[str] = None) -> dict:
    """
    Fetch weather at a ballpark at game time.
    Returns temperature (F), wind_speed (mph), wind_direction, is_indoor.
    """
    is_indoor = venue in INDOOR_PARKS
    lat, lon = PARK_COORDS.get(venue, PARK_COORDS["default"])

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    dirs = hourly.get("wind_direction_10m", [])

    # Find the hour closest to game time; default to 7pm local
    target_hour = 19
    if game_time_utc:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
            target_hour = dt.hour
        except Exception:
            pass

    best_idx = 0
    best_diff = 999
    for i, t in enumerate(times):
        try:
            hour = int(t.split("T")[1].split(":")[0])
            diff = abs(hour - target_hour)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except Exception:
            continue

    temp = temps[best_idx] if temps else 72.0
    wind_speed = winds[best_idx] if winds else 5.0
    wind_deg = dirs[best_idx] if dirs else 0.0

    return {
        "venue": venue,
        "is_indoor": is_indoor,
        "temperature_f": round(temp, 1),
        "wind_speed_mph": round(wind_speed, 1),
        "wind_direction": _wind_direction_label(wind_deg),
        "wind_direction_deg": wind_deg,
        "weather_factor": _compute_weather_factor(venue, temp, wind_speed, wind_deg, is_indoor),
    }


def _compute_weather_factor(
    venue: str, temp: float, wind_speed: float, wind_deg: float, is_indoor: bool
) -> dict:
    """
    Estimate how weather affects run scoring.
    Returns: {total_adjustment: float, notes: list[str]}
    - Positive total_adjustment = more runs expected
    - Negative = fewer runs expected
    """
    if is_indoor:
        return {"total_adjustment": 0.0, "notes": ["Indoor/retractable roof — weather neutral"]}

    notes = []
    adjustment = 0.0

    # Temperature effect: ball travels farther in heat
    if temp >= 85:
        adjustment += 0.4
        notes.append(f"Hot ({temp}F) — ball carries well")
    elif temp <= 45:
        adjustment -= 0.5
        notes.append(f"Cold ({temp}F) — ball dies in the air")
    elif temp <= 55:
        adjustment -= 0.2
        notes.append(f"Cool ({temp}F) — slight suppression")

    # Wind effect: depends on ballpark orientation
    # Most parks have CF roughly pointing away from home plate
    # Wind blowing OUT (toward CF/LCF/RCF) boosts HRs
    # We simplify: if wind > 15 mph, apply direction-based adjustment
    if wind_speed >= 10:
        # Blowing out: S winds at Wrigley, W at most parks → rough heuristic
        # 0=N, 90=E, 180=S, 270=W
        if 135 <= wind_deg <= 225:  # S / SSW / SSE = blowing out at most parks
            boost = min(wind_speed / 10 * 0.3, 1.2)
            adjustment += boost
            notes.append(f"Wind {round(wind_speed)} mph OUT — HR boost")
        elif 315 <= wind_deg or wind_deg <= 45:  # N = blowing in at most parks
            suppress = min(wind_speed / 10 * 0.3, 1.0)
            adjustment -= suppress
            notes.append(f"Wind {round(wind_speed)} mph IN — suppresses scoring")
        else:
            notes.append(f"Wind {round(wind_speed)} mph crosswind — minor effect")

    if not notes:
        notes.append("Weather neutral")

    return {"total_adjustment": round(adjustment, 2), "notes": notes}
