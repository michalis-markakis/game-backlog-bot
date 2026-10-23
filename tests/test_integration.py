"""Integration tests — real handler code with every HTTP call mocked by respx."""
import httpx
import pytest
import respx

# conftest stubs env vars before these imports
from game_bot import metadata, steam, discovery, notion, pipeline

pytestmark = pytest.mark.asyncio


# ── metadata.fetch_page_meta ──────────────────────────────────────────────────

class TestFetchPageMeta:
    @respx.mock
    async def test_youtube_oembed_fast_path(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(
            200, json={"title": "Never Gonna Give You Up", "author_name": "Rick Astley"}))
        respx.get(url).mock(return_value=httpx.Response(200, text="<html></html>"))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Never Gonna Give You Up"
        assert meta["author"] == "Rick Astley"

    @respx.mock
    async def test_opengraph_fallback(self):
        url = "https://example.com/article"
        html = ('<html><head><meta property="og:title" content="Great Title"/>'
                '<meta property="og:description" content="A summary."/></head></html>')
        respx.get(url).mock(return_value=httpx.Response(200, text=html))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Great Title"
        assert meta["desc"] == "A summary."

    @respx.mock
    async def test_jina_fallback_on_generic_title(self):
        """A JS-heavy host with a shell title must be re-read through Jina."""
        url = "https://www.xbox.com/en-US/games/store/forza/9XYZ"
        respx.get(url).mock(return_value=httpx.Response(
            200, text="<html><head><title>Xbox Official Site | Xbox</title></head></html>"))
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(200, json={"data": {
                "title": "Forza Horizon 6", "description": "Race across Japan.", "content": ""}}))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Forza Horizon 6"


class TestJinaRead:
    @respx.mock
    async def test_parses_json_data(self):
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(200, json={"data": {
                "title": "Hades II", "description": "A rogue-like.",
                "content": "Release Date: 6 May, 2024"}}))
        meta = await metadata.jina_read("https://example.com/game")
        assert meta["title"] == "Hades II"
        assert "Release Date" in meta["content"]

    @respx.mock
    async def test_failure_returns_empty(self):
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(500))
        assert await metadata.jina_read("https://example.com/x") == {}


# ── steam ─────────────────────────────────────────────────────────────────────

class TestSteamAppdetails:
    @respx.mock
    async def test_parses_app_data(self):
        payload = {"3714420": {"success": True, "data": {
            "type": "game", "name": "Delta", "developers": ["0xc3pti0n"],
            "genres": [{"description": "Action"}, {"description": "Indie"}, {"description": "Racing"}],
            "release_date": {"coming_soon": True, "date": "2026"},
            "short_description": "A first-person movement platformer about speedrunning.",
            "platforms": {"windows": True, "mac": False, "linux": True},
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))
        out = await steam.appdetails("3714420")
        assert out["name"] == "Delta"
        assert out["developer"] == "0xc3pti0n"
        assert out["platforms"] == ["PC"]
        assert "Racing" in out["genres"]
        assert "Platformer" in out["genres"]          # supplemented from the description
        assert out["coming_soon"] is True
        assert out["release_date"] == "" and out["release_human"] == "2026"
        assert out["store_url"] == "https://store.steampowered.com/app/3714420/"
        assert out["early_access"] is False

    @respx.mock
    async def test_unsuccessful_returns_empty(self):
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json={"999": {"success": False}}))
        assert await steam.appdetails("999") == {}

    @respx.mock
    async def test_detects_early_access_by_genre_id(self):
        payload = {"111": {"success": True, "data": {
            "type": "game", "name": "Some EA Game", "developers": ["Studio"],
            "genres": [{"id": "1", "description": "Action"}, {"id": "70", "description": "Early Access"}],
            "release_date": {"coming_soon": False, "date": "1 Jan, 2025"},
            "short_description": "An early access roguelike.",
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))
        out = await steam.appdetails("111")
        assert out["early_access"] is True
        assert "Early Access" not in out["genres"]     # not surfaced as a Genre tag

    @respx.mock
    async def test_detects_early_access_by_description_without_id(self):
        payload = {"222": {"success": True, "data": {
            "type": "game", "name": "Another EA Game", "developers": ["Studio"],
            "genres": [{"description": "Early Access"}],
            "release_date": {"coming_soon": False, "date": "1 Jan, 2025"},
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))
        out = await steam.appdetails("222")
        assert out["early_access"] is True


# ── pipeline.analyse_game_link (end-to-end, Steam-authoritative path) ─────────

class TestAnalyseGameLinkEarlyAccess:
    @respx.mock
    async def test_steam_early_access_game_is_not_marked_out(self):
        appid = "555"
        respx.post("https://id.twitch.tv/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        respx.post("https://api.igdb.com/v4/games").mock(return_value=httpx.Response(200, json=[]))
        payload = {appid: {"success": True, "data": {
            "type": "game", "name": "EA Game", "developers": ["Studio"],
            "genres": [{"id": "70", "description": "Early Access"}],
            "release_date": {"coming_soon": False, "date": "1 Jan, 2025"},
            "short_description": "An early access game.",
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))

        game = await pipeline.analyse_game_link(
            f"https://store.steampowered.com/app/{appid}/EA_Game/",
            {"title": "EA Game", "desc": ""})
        assert game["status"] == "Early Access"

    @respx.mock
    async def test_steam_fully_released_game_is_out(self):
        appid = "556"
        respx.post("https://id.twitch.tv/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        respx.post("https://api.igdb.com/v4/games").mock(return_value=httpx.Response(200, json=[]))
        payload = {appid: {"success": True, "data": {
            "type": "game", "name": "Released Game", "developers": ["Studio"],
            "genres": [{"id": "1", "description": "Action"}],
            "release_date": {"coming_soon": False, "date": "1 Jan, 2025"},
            "short_description": "A finished game.",
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))

        game = await pipeline.analyse_game_link(
            f"https://store.steampowered.com/app/{appid}/Released_Game/",
            {"title": "Released Game", "desc": ""})
        assert game["status"] == "Out"


class TestSteamSearchAppid:
    @respx.mock
    async def test_prefers_exact_over_playtest(self):
        # Steam ranks the Playtest and an unrelated game above the real one.
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[
                {"appid": "4658140", "name": "over the hill Playtest"},
                {"appid": "396860", "name": "Over The Hills And Far Away"},
                {"appid": "2929250", "name": "over the hill"},
            ]))
        assert await steam.search_appid("over the hill") == "2929250"

    @respx.mock
    async def test_no_match_returns_empty(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "1", "name": "Unrelated Title"}]))
        assert await steam.search_appid("over the hill") == ""


# ── discovery ─────────────────────────────────────────────────────────────────

class TestFindStoreUrl:
    @respx.mock
    async def test_steam_api_exact_match(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "2483190", "name": "Forza Horizon 6"}]))
        assert await discovery.find_store_url("Forza Horizon 6") == \
            "https://store.steampowered.com/app/2483190/"

    @respx.mock
    async def test_falls_back_to_ddg(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "1", "name": "Different Game"}]))
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text="<html>no results</html>"))
        assert await discovery.find_store_url("Forza Horizon 6") == ""


class TestFindReview:
    @respx.mock
    async def test_prefers_metacritic_when_it_resolves(self):
        respx.get(url__regex=r"metacritic\.com/game/").mock(
            return_value=httpx.Response(200, text="<html>reviews</html>"))
        assert await discovery.find_review("Forza Horizon 6") == \
            "https://www.metacritic.com/game/forza-horizon-6/"

    @respx.mock
    async def test_falls_back_to_ddg_when_metacritic_404s(self):
        respx.get(url__regex=r"metacritic\.com/game/").mock(return_value=httpx.Response(404))
        html = ('<a href="/l/?uddg=https%3A%2F%2Fwww.gamespot.com%2Freviews%2F'
                'some-review%2F1900-1%2F">GameSpot</a>')
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text=html))
        out = await discovery.find_review("Some Obscure Game")
        assert "gamespot.com" in out


class TestFindTrailer:
    @respx.mock
    async def test_returns_youtube_link(self):
        html = ('<a href="/l/?uddg=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DGzKT3pIkVmo">'
                'trailer</a>')
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text=html))
        out = await discovery.find_trailer("over the hill")
        assert "youtube.com/watch" in out


# ── notion.find_existing (duplicate guard) ────────────────────────────────────

class TestFindExisting:
    @respx.mock
    async def test_returns_url_when_match(self):
        respx.post(url__regex=r"api\.notion\.com/v1/databases/.*/query").mock(
            return_value=httpx.Response(200, json={"results": [{"url": "https://notion.so/existing"}]}))
        out = await notion.find_existing("Hades II", "https://store.steampowered.com/app/1/")
        assert out == "https://notion.so/existing"

    @respx.mock
    async def test_none_when_no_match(self):
        respx.post(url__regex=r"api\.notion\.com/v1/databases/.*/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        assert await notion.find_existing("Nonexistent Game", "") is None

    @respx.mock
    async def test_filter_ors_store_and_name(self):
        import json as _json
        route = respx.post(url__regex=r"api\.notion\.com/v1/databases/.*/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion.find_existing("Hades II", "https://store.steampowered.com/app/1/")
        body = _json.loads(route.calls[0].request.content)
        props = [c["property"] for c in body["filter"]["or"]]
        assert "Store" in props and "Name" in props

    @respx.mock
    async def test_name_only_filter_when_no_store(self):
        import json as _json
        route = respx.post(url__regex=r"api\.notion\.com/v1/databases/.*/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion.find_existing("Hades II", "")
        body = _json.loads(route.calls[0].request.content)
        assert body["filter"]["property"] == "Name"


# ── save-time duplicate guard ─────────────────────────────────────────────────

from game_bot import bot

class TestSaveTimeGuard:
    async def test_skips_save_when_already_exists(self, monkeypatch):
        calls = {"save": 0, "text": ""}
        async def fake_find(name, store=""): return "https://notion.so/existing"
        async def fake_save(g): calls["save"] += 1; return "https://notion.so/new"
        async def fake_edit(cid, mid, text, kb=None): calls["text"] = text
        monkeypatch.setattr(bot.notion, "find_existing", fake_find)
        monkeypatch.setattr(bot.notion, "save_game", fake_save)
        monkeypatch.setattr(bot.telegram, "edit_buttons", fake_edit)
        await bot._save(1, {"name": "Hades II", "store_url": ""}, 10)
        assert calls["save"] == 0                       # never persisted
        assert "already in your To Play list" in calls["text"]

    async def test_saves_when_not_a_duplicate(self, monkeypatch):
        calls = {"save": 0, "text": ""}
        async def fake_find(name, store=""): return None
        async def fake_save(g): calls["save"] += 1; return "https://notion.so/new"
        async def fake_edit(cid, mid, text, kb=None): calls["text"] = text
        monkeypatch.setattr(bot.notion, "find_existing", fake_find)
        monkeypatch.setattr(bot.notion, "save_game", fake_save)
        monkeypatch.setattr(bot.telegram, "edit_buttons", fake_edit)
        await bot._save(1, {"name": "New Game", "store_url": ""}, 10)
        assert calls["save"] == 1
        assert "Saved to To Play" in calls["text"]


# ── IGDB time to beat ─────────────────────────────────────────────────────────

def _steam_payload(appid, name, coming_soon=False):
    return {appid: {"success": True, "data": {
        "type": "game", "name": name, "developers": ["Studio"],
        "genres": [{"id": "1", "description": "Action"}],
        "release_date": {"coming_soon": coming_soon, "date": "1 Jan, 2025"},
        "short_description": "A game.",
    }}}


class TestTimeToBeat:
    @respx.mock
    async def test_released_game_gets_hours(self):
        from game_bot import igdb
        respx.post("https://id.twitch.tv/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        respx.post("https://api.igdb.com/v4/games").mock(return_value=httpx.Response(
            200, json=[{"id": 42, "name": "Beatable"}]))
        respx.post("https://api.igdb.com/v4/game_time_to_beats").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "normally": 36000}]))
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=_steam_payload("700", "Beatable")))

        game = await pipeline.analyse_game_link(
            "https://store.steampowered.com/app/700/Beatable/", {"title": "Beatable", "desc": ""})
        assert game["time_to_beat"] == 10.0
        assert await igdb.time_to_beat(42) == 10.0

    @respx.mock
    async def test_unreleased_game_skips_lookup(self):
        respx.post("https://id.twitch.tv/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        respx.post("https://api.igdb.com/v4/games").mock(return_value=httpx.Response(
            200, json=[{"id": 43, "name": "Future"}]))
        ttb = respx.post("https://api.igdb.com/v4/game_time_to_beats").mock(
            return_value=httpx.Response(200, json=[{"normally": 3600}]))
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=_steam_payload("701", "Future", coming_soon=True)))

        game = await pipeline.analyse_game_link(
            "https://store.steampowered.com/app/701/Future/", {"title": "Future", "desc": ""})
        assert game["time_to_beat"] is None
        assert not ttb.called

    @respx.mock
    async def test_no_data_returns_none(self):
        from game_bot import igdb
        respx.post("https://id.twitch.tv/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        respx.post("https://api.igdb.com/v4/game_time_to_beats").mock(
            return_value=httpx.Response(200, json=[]))
        assert await igdb.time_to_beat(99) is None


class TestSaveTimeToBeat:
    @respx.mock
    async def test_number_property_written(self):
        route = respx.post("https://api.notion.com/v1/pages").mock(
            return_value=httpx.Response(200, json={"url": "https://notion.so/x"}))
        await notion.save_game({"name": "G", "time_to_beat": 12.5})
        import json
        props = json.loads(route.calls[0].request.content)["properties"]
        assert props["Time to Beat"] == {"number": 12.5}


class TestGamesFeed:
    PAGE = {"id": "p1", "url": "https://notion.so/p1", "created_time": "2026-01-01T00:00:00Z",
            "properties": {
                "Name": {"title": [{"plain_text": "Haste"}]},
                "Developer": {"rich_text": [{"plain_text": "Landfall"}]},
                "Genre": {"multi_select": [{"name": "Racing"}]},
                "Status": {"select": {"name": "Out"}},
                "Release Date": {"date": {"start": "2025-04-01"}},
                "Time to Beat": {"number": 8},
                "Installed": {"checkbox": True},
                "Store": {"url": "https://store.steampowered.com/app/1796470/"}}}

    @respx.mock
    async def test_list_games_flattens_and_paginates(self):
        route = respx.post("https://api.notion.com/v1/databases/game-db-id/query").mock(
            side_effect=[
                httpx.Response(200, json={"results": [self.PAGE], "has_more": True, "next_cursor": "c"}),
                httpx.Response(200, json={"results": [], "has_more": False}),
            ])
        games = await notion.list_games()
        assert route.call_count == 2
        assert games[0]["name"] == "Haste" and games[0]["genres"] == ["Racing"]
        assert games[0]["time_to_beat"] == 8 and games[0]["installed"] is True
        assert games[0]["platforms"] == [] and games[0]["review"] == ""

    async def test_endpoint_rejects_bad_key(self):
        from fastapi.testclient import TestClient
        c = TestClient(bot.app)
        assert c.get("/api/games").status_code == 401
        assert c.get("/api/games", headers={"x-secret-key": "nope"}).status_code == 401


class TestStatusChange:
    PID = "3fa50bcb-4868-81fa-bdb6-cca3e55d5189"
    H = {"x-secret-key": "test-secret"}

    @respx.mock
    async def test_update_status_patches_the_select(self):
        route = respx.patch(f"https://api.notion.com/v1/pages/{self.PID}").mock(
            return_value=httpx.Response(200, json={}))
        await notion.update_status(self.PID, "Playing")
        assert b'"Playing"' in route.calls[0].request.content

    async def test_update_status_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            await notion.update_status(self.PID, "Nope")

    def test_endpoint_requires_key_and_valid_input(self):
        from fastapi.testclient import TestClient
        c = TestClient(bot.app)
        body = {"id": self.PID, "status": "Playing"}
        assert c.post("/api/game-status", json=body).status_code == 401
        assert c.post("/api/game-status", json={**body, "status": "Nope"}, headers=self.H).status_code == 422
        assert c.post("/api/game-status", json={"id": "../users", "status": "Out"}, headers=self.H).status_code == 422

    @respx.mock
    def test_endpoint_saves_status(self):
        from fastapi.testclient import TestClient
        respx.patch(f"https://api.notion.com/v1/pages/{self.PID}").mock(return_value=httpx.Response(200, json={}))
        r = TestClient(bot.app).post("/api/game-status", json={"id": self.PID, "status": "Finished"}, headers=self.H)
        assert r.status_code == 200 and r.json() == {"ok": True}


class TestInstalledFlag:
    PID = "3fa50bcb-4868-81fa-bdb6-cca3e55d5189"
    H = {"x-secret-key": "test-secret"}

    @respx.mock
    async def test_set_installed_patches_the_checkbox(self):
        route = respx.patch(f"https://api.notion.com/v1/pages/{self.PID}").mock(
            return_value=httpx.Response(200, json={}))
        await notion.set_installed(self.PID, True)
        assert b'"Installed"' in route.calls[0].request.content and b"true" in route.calls[0].request.content

    def test_endpoint_requires_key_and_valid_input(self):
        from fastapi.testclient import TestClient
        c = TestClient(bot.app)
        assert c.post("/api/game-installed", json={"id": self.PID, "installed": True}).status_code == 401
        assert c.post("/api/game-installed", json={"id": "../x", "installed": True}, headers=self.H).status_code == 422
        assert c.post("/api/game-installed", json={"id": self.PID, "installed": "maybe"}, headers=self.H).status_code == 422

    @respx.mock
    def test_endpoint_saves_flag(self):
        from fastapi.testclient import TestClient
        respx.patch(f"https://api.notion.com/v1/pages/{self.PID}").mock(return_value=httpx.Response(200, json={}))
        r = TestClient(bot.app).post("/api/game-installed", json={"id": self.PID, "installed": False}, headers=self.H)
        assert r.status_code == 200 and r.json() == {"ok": True}


class TestGameMedia:
    H = {"x-secret-key": "test-secret"}
    STEAM = "https://store.steampowered.com/api/appdetails"

    @respx.mock
    async def test_media_picks_the_highlighted_clip_over_https(self):
        respx.get(self.STEAM).mock(return_value=httpx.Response(200, json={"1796470": {"success": True, "data": {"movies": [
            {"thumbnail": "http://cdn/p.jpg", "mp4": {"480": "http://cdn/m480.mp4", "max": "http://cdn/max.mp4"},
             "webm": {"480": "http://cdn/m480.webm"}}]}}}))
        m = await steam.media("1796470")
        assert m == {"video": "https://cdn/m480.mp4", "webm": "https://cdn/m480.webm", "poster": "https://cdn/p.jpg"}

    @respx.mock
    async def test_media_empty_when_no_trailer_or_error(self):
        respx.get(self.STEAM).mock(return_value=httpx.Response(200, json={"1": {"success": True, "data": {}}}))
        assert await steam.media("1") == {}
        respx.get(self.STEAM).mock(return_value=httpx.Response(500))
        assert await steam.media("1") == {}
        assert await steam.media("../x") == {}

    @respx.mock
    def test_endpoint_needs_key_and_a_numeric_appid_and_caches(self):
        from fastapi.testclient import TestClient
        bot._MEDIA.clear()
        route = respx.get(self.STEAM).mock(return_value=httpx.Response(200, json={"42": {"success": True, "data": {"movies": [
            {"mp4": {"480": "https://cdn/a.mp4"}}]}}}))
        c = TestClient(bot.app)
        assert c.get("/api/game-media?appid=42").status_code == 401
        assert c.get("/api/game-media?appid=abc", headers=self.H).status_code == 422
        assert c.get("/api/game-media?appid=42", headers=self.H).json()["video"] == "https://cdn/a.mp4"
        c.get("/api/game-media?appid=42", headers=self.H)
        assert route.call_count == 1
