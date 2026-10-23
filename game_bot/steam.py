"""
Steam store client. The public storefront API needs no key, and the appid is an
unambiguous identifier — so whenever a game is on Steam this is the most reliable
source for its name, developer, genres, and release date.
"""
from __future__ import annotations
import re
import httpx
from urllib.parse import quote

from .text_utils import norm_name
from . import mapping

# Auxiliary Steam apps to skip when matching a game (playtests, demos, soundtracks…)
_AUX = ("playtest", "demo", "beta", "soundtrack", "ost", "server",
        "sdk", "artbook", "bonus", "dedicated")


def _is_aux(name: str) -> bool:
    low = (name or "").lower()
    return any(x in low for x in _AUX)


def appid_from_url(url: str) -> str:
    """Extract the numeric appid from a store.steampowered.com/app/<id>/ URL."""
    m = re.search(r"store\.steampowered\.com/app/(\d+)", url or "")
    return m.group(1) if m else ""


async def search_appid(game_name: str) -> str:
    """
    Best Steam appid for a game name. Steam's search ranks playtests/demos above
    the real game for short titles, so prefer an exact (normalised) name match and
    skip auxiliary apps. Returns '' on miss.
    """
    if not game_name:
        return ""
    target = norm_name(game_name)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://steamcommunity.com/actions/SearchApps/{quote(game_name)}")
        if r.status_code != 200:
            return ""
        apps = [a for a in (r.json() or [])
                if isinstance(a, dict) and a.get("appid") and a.get("name")]
    except Exception:
        return ""
    # 1. exact normalised match, not an auxiliary app
    for a in apps:
        if norm_name(a["name"]) == target and not _is_aux(a["name"]):
            return a["appid"]
    # 2. exact normalised match even if auxiliary
    for a in apps:
        if norm_name(a["name"]) == target:
            return a["appid"]
    # 3. non-auxiliary partial match (the search target appears in the name)
    for a in apps:
        if not _is_aux(a["name"]) and target and target in norm_name(a["name"]):
            return a["appid"]
    return ""


async def appdetails(appid: str) -> dict:
    """
    Authoritative game data from Steam's store API for a known appid (no key).
    Returns {} for a missing app, a non-game entry, or any error.
    """
    if not appid:
        return {}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get("https://store.steampowered.com/api/appdetails",
                                 params={"appids": appid, "l": "english"})
        if r.status_code != 200:
            return {}
        entry = (r.json() or {}).get(str(appid), {})
        if not entry.get("success"):
            return {}
        data = entry.get("data", {})
        if data.get("type") != "game":
            return {}
        devs = data.get("developers") or []
        raw_genres = [g for g in data.get("genres", []) if isinstance(g, dict)]
        genres = mapping.map_genres([g.get("description", "") for g in raw_genres])
        # Supplement coarse Steam tags with distinctive subgenres from the blurb.
        blob = f"{data.get('name', '')} {data.get('short_description', '')}".lower()
        for kw, g in mapping.DESC_GENRE_HINTS.items():
            if g not in genres and kw in blob:
                genres.append(g)
        # Steam tags Early Access titles with genre id 70 ("Early Access") — check the
        # raw genres (before map_genres, which has no mapping for it and drops it).
        early_access = any(
            g.get("id") == "70" or (g.get("description") or "").strip().lower() == "early access"
            for g in raw_genres)
        rd = data.get("release_date") or {}
        date_str = (rd.get("date") or "").strip()
        exact = mapping.parse_exact_date(date_str)
        return {
            "name":          data.get("name", ""),
            "developer":     devs[0] if devs else "",
            "genres":        genres,
            "platforms":     ["PC"],                       # Steam only reports desktop
            "release_date":  exact,
            "release_human": "" if exact else date_str,
            "coming_soon":   bool(rd.get("coming_soon")),
            "early_access":  early_access,
            "summary":       (data.get("short_description") or "")[:500],
            "store_url":     f"https://store.steampowered.com/app/{appid}/",
        }
    except Exception as e:
        print(f"[steam] appdetails failed: {e}")
        return {}


def _https(url: str) -> str:
    """Steam still returns some media URLs over http; the dashboard is served over https."""
    return "https://" + url[len("http://"):] if (url or "").startswith("http://") else (url or "")


async def media(appid: str) -> dict:
    """
    The game's highlighted Steam trailer as a short looping clip: {video, webm, poster}.
    Returns {} when the app has no mp4/webm trailer (the dashboard then keeps the still image).
    """
    if not str(appid).isdigit():
        return {}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get("https://store.steampowered.com/api/appdetails",
                                 params={"appids": appid, "l": "english"})
        if r.status_code != 200:
            return {}
        entry = (r.json() or {}).get(str(appid), {})
        movies = (entry.get("data") or {}).get("movies") or [] if entry.get("success") else []
    except Exception as e:
        print(f"[steam] media failed: {e}")
        return {}
    for m in movies:
        mp4, webm = m.get("mp4") or {}, m.get("webm") or {}
        video = _https(mp4.get("480") or mp4.get("max") or "")
        alt = _https(webm.get("480") or webm.get("max") or "")
        if video or alt:
            return {"video": video, "webm": alt, "poster": _https(m.get("thumbnail") or "")}
    return {}
