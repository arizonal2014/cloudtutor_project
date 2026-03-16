from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents.llm_agent import Agent

CITY_TO_TZ = {
    "stockholm": "Europe/Stockholm",
    "new york": "America/New_York",
    "san francisco": "America/Los_Angeles",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
}


def get_current_time(city: str) -> dict:
    """Returns the current local time for a supported city."""
    normalized_city = city.strip().lower()
    timezone_name = CITY_TO_TZ.get(normalized_city)
    if not timezone_name:
        return {
            "status": "error",
            "message": f"Unsupported city: {city}",
            "supported_cities": sorted(CITY_TO_TZ.keys()),
        }

    now = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "status": "success",
        "city": city,
        "timezone": timezone_name,
        "time": now,
    }


root_agent = Agent(
    model="gemini-2.5-flash",
    name="root_agent",
    description="Tells the current time in supported cities.",
    instruction=(
        "You are a helpful assistant that tells the current time in cities. "
        "Use the 'get_current_time' tool whenever a user asks for local time."
    ),
    tools=[get_current_time],
)
