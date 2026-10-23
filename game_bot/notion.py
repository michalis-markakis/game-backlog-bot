"""Persist a resolved game to the Notion "To Play" database."""
from __future__ import annotations
import httpx

from .config import NOTION_HEADERS, NOTION_DB_GAME
from .text_utils import rich_text

_STATUSES = ("Unreleased", "Early Access", "Out", "Playing", "Finished", "Abandoned")
_HYPE = ("★★★", "★★", "★")


async def find_existing(name: str, store_url: str = "") -> str | None:
    """
    Return the Notion URL of a To Play entry that already covers this game, or None.

    Matches on the Store URL (the same Steam appid is unambiguously the same game)
    or an exact Name — so re-sharing a game doesn't create a duplicate.
    """
    if not NOTION_DB_GAME:
        return None
    conditions = []
    if store_url:
        conditions.append({"property": "Store", "url": {"equals": store_url}})
    if name:
        conditions.append({"property": "Name", "title": {"equals": name}})
    if not conditions:
        return None
    filt = conditions[0] if len(conditions) == 1 else {"or": conditions}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_GAME}/query",
                headers=NOTION_HEADERS, json={"filter": filt, "page_size": 1})
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        return results[0].get("url") if results else None
    except Exception as e:
        print(f"[notion] duplicate check failed: {e}")
        return None


async def save_game(game: dict) -> str:
    """Create a page in the To Play database and return its Notion URL."""
    props: dict = {"Name": {"title": [{"text": {"content": (game.get("name") or "")[:200]}}]}}

    for field, key in (("Review", "review_url"), ("Video", "video_url"), ("Store", "store_url")):
        if game.get(key):
            props[field] = {"url": game[key]}
    if game.get("developer"):
        props["Developer"] = {"rich_text": rich_text(game["developer"])}
    if game.get("genres"):
        props["Genre"] = {"multi_select": [{"name": g} for g in game["genres"]]}
    if game.get("platforms"):
        props["Platform"] = {"multi_select": [{"name": p} for p in game["platforms"]]}
    if game.get("status") in _STATUSES:
        props["Status"] = {"select": {"name": game["status"]}}
    if game.get("hype") in _HYPE:
        props["Hype"] = {"select": {"name": game["hype"]}}
    if game.get("time_to_beat"):
        props["Time to Beat"] = {"number": game["time_to_beat"]}
    if game.get("release_date"):
        props["Release Date"] = {"date": {"start": game["release_date"]}}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS,
                              json={"parent": {"database_id": NOTION_DB_GAME}, "properties": props})
    r.raise_for_status()
    return r.json()["url"]


def _flatten_game(page: dict) -> dict:
    """Reduce a Notion page to the plain fields the dashboard renders."""
    p = page.get("properties", {})

    def text(key: str) -> str:
        return "".join(t.get("plain_text", "") for t in (p.get(key) or {}).get("rich_text", []))

    def select(key: str) -> str:
        return ((p.get(key) or {}).get("select") or {}).get("name", "")

    def multi(key: str) -> list[str]:
        return [o["name"] for o in (p.get(key) or {}).get("multi_select", [])]

    title = "".join(t.get("plain_text", "") for t in (p.get("Name") or {}).get("title", []))
    return {
        "id": page["id"],
        "url": page.get("url", ""),
        "name": title,
        "developer": text("Developer"),
        "genres": multi("Genre"),
        "platforms": multi("Platform"),
        "status": select("Status"),
        "hype": select("Hype"),
        "rating": select("My Rating"),
        "release_date": ((p.get("Release Date") or {}).get("date") or {}).get("start") or "",
        "time_to_beat": (p.get("Time to Beat") or {}).get("number"),
        "installed": bool((p.get("Installed") or {}).get("checkbox")),
        "store": (p.get("Store") or {}).get("url") or "",
        "review": (p.get("Review") or {}).get("url") or "",
        "video": (p.get("Video") or {}).get("url") or "",
        "added": page.get("created_time", ""),
    }


async def list_games() -> list[dict]:
    """Return every game in the To Play database (paginated), newest first."""
    games: list[dict] = []
    cursor = None
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            body: dict = {"page_size": 100,
                          "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
            if cursor:
                body["start_cursor"] = cursor
            r = await client.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_GAME}/query",
                headers=NOTION_HEADERS, json=body)
            r.raise_for_status()
            data = r.json()
            games.extend(_flatten_game(pg) for pg in data.get("results", []))
            if not data.get("has_more"):
                return games
            cursor = data.get("next_cursor")


async def set_installed(page_id: str, installed: bool) -> None:
    """Tick or untick the Installed checkbox on an existing To Play page."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS,
                               json={"properties": {"Installed": {"checkbox": installed}}})
    r.raise_for_status()


async def update_status(page_id: str, status: str) -> None:
    """Set the Status select on an existing To Play page."""
    if status not in _STATUSES:
        raise ValueError(f"unknown status: {status}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS,
                               json={"properties": {"Status": {"select": {"name": status}}}})
    r.raise_for_status()
