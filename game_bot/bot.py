"""
FastAPI application and Telegram interaction layer.

Flow: a message with a link → analyse it → post a confirmation card with a
"how hyped?" rating → the button tap saves the game to Notion. Pending cards are
held in an in-process dict, which is fine for a single-user bot.
"""
from __future__ import annotations
import re
import uuid
import hmac
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field

from .config import TELEGRAM_USER_ID, NOTION_DB_GAME, SECRET_KEY
from .text_utils import clean_field
from . import telegram, metadata, pipeline, notion, mapping, discovery, steam

app = FastAPI(title="Game Backlog Bot")


@app.middleware("http")
async def _restore_original_path(request: Request, call_next):
    """
    Vercel rewrites every /api/* request to the single /api/index function and
    (per vercel.json) carries the original sub-path as ?__p=... — the function
    otherwise only ever sees '/api/index'. Restore the real path so the router can
    match /api/telegram, /api/health, etc.
    """
    p = request.query_params.get("__p")
    if p is not None:
        real = "/api/" + p
        request.scope["path"] = real
        request.scope["raw_path"] = real.encode("utf-8")
    return await call_next(request)


# Pending confirmation cards, keyed by a short id embedded in the button callbacks.
PENDING: dict = {}

_VALID_STATUS = ("Unreleased", "Early Access", "Out", "Playing", "Finished", "Abandoned")
_HYPE_BY_LEVEL = {"3": "★★★", "2": "★★", "1": "★"}

HELP_TEXT = (
    "🎮 *Game Backlog Bot*\n\n"
    "Send me a link to a game — a *trailer*, a *review*, or a *store page* — and I'll "
    "identify it, pull the developer, genres, platforms and release date, find its store "
    "page and a review, then ask how hyped you are before saving it to your *To Play* "
    "Notion database\\.\n\n"
    "Add a note by putting text after the link\\."
)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/api/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()

    if "callback_query" in data:
        await handle_callback(data["callback_query"])
        return {"ok": True}

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_id = str(message.get("from", {}).get("id", ""))
    text = message.get("text", "").strip()

    if user_id != TELEGRAM_USER_ID or not text or not chat_id:
        return {"ok": True}

    if text in ("/start", "/help"):
        await telegram.send(chat_id, HELP_TEXT)
    elif (m := re.search(r"https?://\S+", text)):
        url = m.group(0)
        note = text.replace(url, "").strip()
        await handle_game_link(chat_id, url, note)
    else:
        await telegram.send(chat_id, "Send me a game link (trailer, review, or store "
                                     "page) and I'll add it to your backlog. /help for details.")
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "v": "early-access-1"}


def _require_key(request: Request) -> None:
    """Guard for the dashboard endpoints: the caller must present SECRET_KEY."""
    given = request.headers.get("x-secret-key", "")
    if not hmac.compare_digest(given.encode(), SECRET_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid key")
    if not NOTION_DB_GAME:
        raise HTTPException(status_code=503, detail="NOTION_DB_GAME not configured")


@app.get("/api/games")
async def games(request: Request):
    """Read-only feed for the dashboard in public/index.html; guarded by SECRET_KEY."""
    _require_key(request)
    try:
        return {"games": await notion.list_games()}
    except Exception as e:
        print(f"[notion] list failed: {e}")
        raise HTTPException(status_code=502, detail="Could not read Notion")


_PAGE_ID = r"^[0-9a-fA-F-]{32,36}$"                      # a Notion page id, nothing else


class StatusChange(BaseModel):
    id: str = Field(pattern=_PAGE_ID)
    status: str


class InstalledChange(BaseModel):
    id: str = Field(pattern=_PAGE_ID)
    installed: bool


@app.post("/api/game-status")
async def game_status(change: StatusChange, request: Request):
    """Change one game's Status from the dashboard's side panel."""
    _require_key(request)
    if change.status not in _VALID_STATUS:
        raise HTTPException(status_code=422, detail=f"Status must be one of: {', '.join(_VALID_STATUS)}")
    try:
        await notion.update_status(change.id, change.status)
    except Exception as e:
        print(f"[notion] status update failed: {e}")
        raise HTTPException(status_code=502, detail="Could not save to Notion")
    return {"ok": True}


@app.post("/api/game-installed")
async def game_installed(change: InstalledChange, request: Request):
    """Tick or untick a game's Installed flag from the dashboard's side panel."""
    _require_key(request)
    try:
        await notion.set_installed(change.id, change.installed)
    except Exception as e:
        print(f"[notion] installed update failed: {e}")
        raise HTTPException(status_code=502, detail="Could not save to Notion")
    return {"ok": True}


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_game_link(chat_id: int, url: str, note: str) -> None:
    """Analyse a shared link and present a confirmation card with hype buttons."""
    if not NOTION_DB_GAME:
        await telegram.send(chat_id, "⚠️ Game database not configured. Set NOTION_DB_GAME.")
        return

    meta = await metadata.fetch_page_meta(url)
    try:
        game = await pipeline.analyse_game_link(url, meta)
    except Exception as e:
        await telegram.send(chat_id, f"❌ Game analysis failed: {e}")
        return

    name = clean_field(game.get("name", "")) or meta.get("title", "") or url
    developer = clean_field(game.get("developer", ""))
    genres = mapping.map_genres(game.get("genres") or [])
    platforms = mapping.map_platforms(game.get("platforms") or [])
    status = game.get("status", "Out")
    if status not in _VALID_STATUS:
        status = "Out"
    release_date = clean_field(game.get("release_date", ""))     # exact ISO, else ""
    release_human = clean_field(game.get("release_human", ""))   # approx text, else ""
    store_url = game.get("store_url", "")
    summary = game.get("summary", "")
    if note:
        summary = note + (" — " + summary if summary else "")

    # Don't add a game that's already in the backlog (matched by store URL or name).
    dedup_store = store_url or (url if mapping.is_game_url(url) else "")
    existing = await notion.find_existing(name, dedup_store)
    if existing:
        await telegram.send(
            chat_id, f"✋ *{name}* is already in your To Play list.\n[Open in Notion]({existing})")
        return

    store_url, video_url, review_url = await _resolve_links(url, name, store_url)

    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {
        "chat_id": chat_id, "name": name[:200], "developer": developer,
        "genres": genres, "platforms": platforms, "status": status,
        "release_date": release_date, "store_url": store_url,
        "review_url": review_url, "video_url": video_url,
        "time_to_beat": game.get("time_to_beat"),
    }
    await telegram.send_buttons(
        chat_id,
        _card(name, developer, genres, platforms, status, release_date,
              release_human, store_url, video_url, review_url, summary,
              game.get("time_to_beat")),
        _hype_keyboard(pid),
    )


async def _resolve_links(url: str, name: str, store_url: str) -> tuple[str, str, str]:
    """Decide which link is the trailer vs. review, and auto-find whatever's missing."""
    if mapping.is_video_url(url):
        video_url, review_url = mapping.clean_video_url(url), ""
    elif mapping.is_game_url(url):
        store_url = store_url or url
        video_url = await discovery.find_trailer(name) if name else ""
        review_url = ""
    else:
        review_url = url
        video_url = await discovery.find_trailer(name) if name else ""
    video_url = mapping.clean_video_url(video_url) if video_url else ""

    if not store_url and name:
        store_url = await discovery.find_store_url(name)
    if not review_url and name:
        review_url = await discovery.find_review(name)
    # No critic review (e.g. an unreleased game) → link Steam's own reviews page,
    # which is guaranteed to be the right game because it uses the exact appid.
    if not review_url:
        appid = steam.appid_from_url(store_url)
        if appid:
            review_url = f"https://steamcommunity.com/app/{appid}/reviews/"
    return store_url, video_url, review_url


async def handle_callback(cq: dict) -> None:
    """Handle inline-button taps: hype:<pid>:<level>, save:<pid>, cancel:<pid>."""
    await telegram.answer_callback(cq["id"])
    if str(cq["from"]["id"]) != TELEGRAM_USER_ID:
        return

    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    action, _, rest = data.partition(":")

    if action == "cancel":
        PENDING.pop(rest, None)
        await telegram.edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Cancelled._")
        return

    pid, _, level = rest.partition(":")
    pending = PENDING.pop(pid, None)
    if not pending:
        await telegram.edit_buttons(chat_id, message_id, "⏱ Action expired — send the link again.")
        return
    if action == "hype":
        pending["hype"] = _HYPE_BY_LEVEL.get(level, "★★")
    await telegram.edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Saving…_")
    await _save(chat_id, pending, message_id)


async def _save(chat_id: int, pending: dict, message_id: int) -> None:
    # Re-check at save time in case the same game was shared and saved from another
    # card between analysis and this tap.
    existing = await notion.find_existing(pending["name"], pending.get("store_url", ""))
    if existing:
        await telegram.edit_buttons(
            chat_id, message_id,
            f"✋ *{pending['name']}* is already in your To Play list.\n[Open in Notion]({existing})")
        return
    try:
        notion_url = await notion.save_game(pending)
    except Exception as e:
        await telegram.edit_buttons(chat_id, message_id, f"❌ Failed to save: {e}")
        return
    reply = f"🎮 *Saved to To Play*\n*{pending['name']}*"
    if pending.get("developer"):
        reply += f"\n👨‍💻 {pending['developer']}"
    if pending.get("genres"):
        reply += "\n🏷️ " + "  ·  ".join(pending["genres"])
    reply += f"\n\n[Open in Notion]({notion_url})"
    await telegram.edit_buttons(chat_id, message_id, reply)


# ── Card rendering ────────────────────────────────────────────────────────────

def _card(name, developer, genres, platforms, status, release_date,
          release_human, store_url, video_url, review_url, summary, time_to_beat=None) -> str:
    lines = [f"🎮 *{name}*"]
    if developer:
        lines.append(f"👨‍💻 {developer}")
    if genres:
        lines.append("🏷️ " + "  ·  ".join(genres))
    if platforms:
        lines.append("🖥️ " + "  ·  ".join(platforms))
    detail = status
    if release_date:
        detail += f"  ·  📅 {release_date}"
    elif release_human:
        detail += f"  ·  📅 ~{release_human}"
    lines.append(detail)
    if time_to_beat:
        lines.append(f"⏱️ ~{time_to_beat:g}h to beat")
    if store_url:
        lines.append(f"🛒 [Store]({store_url})")
    if video_url:
        lines.append(f"🎬 [Trailer]({video_url})")
    if review_url:
        lines.append(f"📝 [Review]({review_url})")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n*How hyped?*")
    return "\n".join(lines)


def _hype_keyboard(pid: str) -> list:
    return [
        [
            {"text": "🔥 ★★★", "callback_data": f"hype:{pid}:3"},
            {"text": "⭐ ★★",  "callback_data": f"hype:{pid}:2"},
            {"text": "🙂 ★",   "callback_data": f"hype:{pid}:1"},
        ],
        [
            {"text": "💾 Save (no hype)", "callback_data": f"save:{pid}"},
            {"text": "❌ Cancel",         "callback_data": f"cancel:{pid}"},
        ],
    ]
