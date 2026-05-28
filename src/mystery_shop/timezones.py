"""US state → IANA timezone. Split states (FL, KY, TN, IN, ND, SD, OR, NE, KS, TX, MI, ID)
pick the dominant timezone for the population center. Documented as a known simplification
in the README. For a real system you'd geocode against the postal code or a TZ shapefile."""
from __future__ import annotations

STATE_TO_TZ: dict[str, str] = {
    # Eastern
    "Connecticut": "America/New_York", "Delaware": "America/New_York",
    "District of Columbia": "America/New_York", "Georgia": "America/New_York",
    "Maine": "America/New_York", "Maryland": "America/New_York",
    "Massachusetts": "America/New_York", "New Hampshire": "America/New_York",
    "New Jersey": "America/New_York", "New York": "America/New_York",
    "North Carolina": "America/New_York", "Ohio": "America/New_York",
    "Pennsylvania": "America/New_York", "Rhode Island": "America/New_York",
    "South Carolina": "America/New_York", "Vermont": "America/New_York",
    "Virginia": "America/New_York", "West Virginia": "America/New_York",
    # Split states defaulting to Eastern (majority population)
    "Florida": "America/New_York",   # panhandle is Central
    "Indiana": "America/New_York",   # NW corner is Central
    "Kentucky": "America/New_York",  # western half is Central
    "Michigan": "America/New_York",  # UP corner is Central
    "Tennessee": "America/Chicago",  # majority Central (Memphis, Nashville)
    # Central
    "Alabama": "America/Chicago", "Arkansas": "America/Chicago",
    "Illinois": "America/Chicago", "Iowa": "America/Chicago",
    "Louisiana": "America/Chicago", "Minnesota": "America/Chicago",
    "Mississippi": "America/Chicago", "Missouri": "America/Chicago",
    "Oklahoma": "America/Chicago", "Wisconsin": "America/Chicago",
    "Kansas": "America/Chicago", "Nebraska": "America/Chicago",
    "North Dakota": "America/Chicago", "South Dakota": "America/Chicago",
    "Texas": "America/Chicago",  # El Paso is Mountain
    # Mountain
    "Colorado": "America/Denver", "Montana": "America/Denver",
    "New Mexico": "America/Denver", "Utah": "America/Denver",
    "Wyoming": "America/Denver",
    "Arizona": "America/Phoenix",  # no DST
    "Idaho": "America/Denver",     # panhandle is Pacific
    # Pacific
    "California": "America/Los_Angeles", "Nevada": "America/Los_Angeles",
    "Washington": "America/Los_Angeles", "Oregon": "America/Los_Angeles",
    # Alaska/Hawaii
    "Alaska": "America/Anchorage", "Hawaii": "Pacific/Honolulu",
}


def state_to_timezone(state: str | None) -> str | None:
    if not state:
        return None
    return STATE_TO_TZ.get(state.strip())
