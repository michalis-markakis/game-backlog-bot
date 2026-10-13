"""
Sanitize the Notion "To Play" database.

`plan_fixes` is pure: given one page's properties it returns the safe, deterministic
fixes to apply plus warnings for things that need a human call. `run` walks the whole
database, applies the fixes, and returns a summary — meant to be triggered by a
scheduler via `/api/sanitize`.

Only unambiguous fixes are automated. Anything that needs judgement (e.g. an
"Unreleased" game whose date has passed could be Out or Early Access) is reported,
never guessed.
"""
from __future__ import annotations
import re
from datetime import date

import httpx

from .config import NOTION_HEADERS, NOTION_DB_GAME
from .steam import appid_from_url
from .text_utils import rich_text

_STEAM_REVIEWS = re.compile(r"steamcommunity\.com/app/(\d+)")


def _plain(prop: dict, kind: str) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get(kind) or [])


def plan_fixes(props: dict, today: date) -> tuple[dict, list[str]]:
    """Return (properties patch, warnings) for one page's Notion properties."""
    patch: dict = {}
    warnings: list[str] = []

    name = _plain(props.get("Name", {}), "title")
    if name and name != name.strip():
        patch["Name"] = {"title": [{"text": {"content": name.strip()[:200]}}]}

    developer = _plain(props.get("Developer", {}), "rich_text")
    tidy = re.sub(r"^by\s+", "", developer.strip(), flags=re.I)
    if developer and tidy != developer:
        patch["Developer"] = {"rich_text": rich_text(tidy)}
    if not tidy:
        warnings.append("missing Developer")

    # The bot derives the Steam reviews link from the store page's exact appid, so
    # when the two disagree the Store link is the one that's wrong.
    store = props.get("Store", {}).get("url") or ""
    review = props.get("Review", {}).get("url") or ""
    m = _STEAM_REVIEWS.search(review)
    store_id = appid_from_url(store)
    if m and store_id and m.group(1) != store_id:
        patch["Store"] = {"url": f"https://store.steampowered.com/app/{m.group(1)}/"}

    if not props.get("Genre", {}).get("multi_select"):
        warnings.append("missing Genre")

    status = (props.get("Status", {}).get("select") or {}).get("name")
    released = ((props.get("Release Date", {}).get("date") or {}).get("start") or "")[:10]
    if status == "Unreleased" and released and released < today.isoformat():
        warnings.append(f"Unreleased but release date {released} has passed")

    return patch, warnings


async def run(today: date | None = None) -> dict:
    """Sanitize every page in the To Play database; return a summary of what changed."""
    today = today or date.today()
    fixed: list[dict] = []
    warned: list[dict] = []
    cursor = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = await client.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_GAME}/query",
                headers=NOTION_HEADERS, json=body)
            r.raise_for_status()
            data = r.json()
            for page in data["results"]:
                props = page["properties"]
                title = _plain(props.get("Name", {}), "title") or page["id"]
                patch, warnings = plan_fixes(props, today)
                if patch:
                    u = await client.patch(
                        f"https://api.notion.com/v1/pages/{page['id']}",
                        headers=NOTION_HEADERS, json={"properties": patch})
                    u.raise_for_status()
                    fixed.append({"name": title, "fields": sorted(patch)})
                if warnings:
                    warned.append({"name": title, "warnings": warnings})
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
    return {"fixed": fixed, "needs_review": warned}
