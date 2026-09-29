"""YouTube player state and the background watcher (Remote page).

Everything about the player is READ off the kiosk tab over DevTools
(app/cdp.py JS_STATE): playing / paused / buffering / ad / ended /
live, plus YouTube's walls (bot check, sign-in, age gate, "Video paused.
Continue watching?"). phase() turns that into one word with guidance
and one-tap actions - never assumed.

The watcher runs inside the dashboard every few seconds while a web
source is active and the kiosk answers, and does only what the
operator switched on (settings): skip a skippable ad, dismiss the
inactivity prompt, and advance to the next saved link when a video
ends. Each automatic action is audited once per occurrence. Loop is set on the
<video> element right after a play (main.play_youtube_link)."""
from __future__ import annotations

import asyncio
import json
import time

from . import cdp, db

SETTING_KEYS = {"yt_auto_advance": "0", "yt_auto_skip_ads": "1", "yt_auto_dismiss_prompts": "1", "yt_keep_fullscreen": "1"}

# In-memory playback context: which saved link the kiosk was last sent to
# (for next/prev/auto-advance) and the last state seen by the watcher.
current: dict = {"link_id": None, "url": None, "since": None}
last: dict = {"state": None, "at": 0.0, "auto": []}
_auto_marks: dict = {}   # de-dup automatic actions per (kind, url)


def get_settings() -> dict:
    return {k: db.get_setting(k, v) == "1" for k, v in SETTING_KEYS.items()}


def set_settings(values: dict) -> dict:
    for k in SETTING_KEYS:
        if k in values:
            db.set_setting(k, "1" if values[k] else "0")
    return get_settings()


def list_links() -> list[dict]:
    from . import youtube
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM youtube_links ORDER BY sort_order, id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["options"] = json.loads(d["options"]) if d.get("options") else {}
        except (json.JSONDecodeError, TypeError):
            d["options"] = {}
        info = youtube.classify(d["url"])
        d["kind"] = d.get("kind") or (info["kind"] if info["kind"] in youtube.KINDS else "video")
        d["thumbnail_url"] = d.get("thumbnail_url") or youtube.thumbnail_url(d["url"])
        d["video_id"] = info.get("video_id")
        out.append(d)
    return out


def neighbour(direction: int) -> dict | None:
    """The saved link after/before the current one (wrapping), or the
    first when nothing is current."""
    links = list_links()
    if not links:
        return None
    ids = [l["id"] for l in links]
    if current["link_id"] not in ids:
        return links[0] if direction >= 0 else links[-1]
    i = (ids.index(current["link_id"]) + direction) % len(links)
    return links[i]


# ---------------------------------------------------------------- phase + guidance

PHASES = {
    "idle":      {"label": "Nothing playing", "tone": "idle"},
    "loading":   {"label": "Loading", "tone": "wait"},
    "playing":   {"label": "Playing", "tone": "ok"},
    "paused":    {"label": "Paused", "tone": "idle"},
    "buffering": {"label": "Buffering", "tone": "wait"},
    "ad":        {"label": "Ad playing", "tone": "wait"},
    "ended":     {"label": "Ended", "tone": "idle"},
    "prompt":    {"label": "\"Continue watching?\" prompt", "tone": "wait"},
    "bot_check": {"label": "Bot check wall", "tone": "bad"},
    "signin":    {"label": "Sign-in required", "tone": "bad"},
    "age":       {"label": "Age-restricted", "tone": "bad"},
    "error":     {"label": "Playback error", "tone": "bad"},
    "no_player": {"label": "No player on this page", "tone": "idle"},
    "offline":   {"label": "Browser not reachable", "tone": "bad"},
}


def phase(state: dict | None) -> dict:
    """One word for what the player is doing, with what to do about it."""
    if not state or not state.get("available"):
        return {"phase": "offline", **PHASES["offline"], "guide": (state or {}).get("reason") or "The kiosk browser isn't answering.", "actions": ["restart-browser"]}
    hint = state.get("body_hint") or ""
    if state.get("continue_prompt"):
        return {"phase": "prompt", **PHASES["prompt"], "guide": "YouTube paused for inactivity and is asking whether to keep playing. Auto-dismiss handles this when it is on.", "actions": ["dismiss-prompt"]}
    if hint == "bot":
        return {"phase": "bot_check", **PHASES["bot_check"], "guide": "\"Sign in to confirm you're not a bot\" - YouTube won't play from this datacenter IP without a signed-in session. Play as a signed-in account.", "actions": ["play-as", "accounts"]}
    if hint == "age":
        return {"phase": "age", **PHASES["age"], "guide": "This video needs a signed-in, age-verified account.", "actions": ["play-as", "accounts"]}
    if hint == "signin":
        return {"phase": "signin", **PHASES["signin"], "guide": "YouTube is asking for a sign-in before it will play this.", "actions": ["play-as", "accounts"]}
    if hint == "error" or (state.get("error_text") and not state.get("has_video")):
        return {"phase": "error", **PHASES["error"], "guide": (state.get("error_text") or "The player reported an error.")[:200] + " Retry, or check the link in a normal browser.", "actions": ["retry", "next"]}
    if not state.get("has_video"):
        return {"phase": "no_player", **PHASES["no_player"], "guide": "The browser is on a page without a video player. Pick something from the library.", "actions": []}
    if state.get("ad_showing"):
        return {"phase": "ad", **PHASES["ad"], "guide": "An ad is playing. Skip it when the button appears (automatic when Skip ads is on)." if not state.get("skippable") else "A skippable ad is playing.", "actions": ["skip-ad"] if state.get("skippable") else []}
    if state.get("ended"):
        return {"phase": "ended", **PHASES["ended"], "guide": "The video finished. Replay it, or play the next saved link (automatic when Auto-advance is on).", "actions": ["replay", "next"]}
    if state.get("paused"):
        return {"phase": "paused", **PHASES["paused"], "guide": "", "actions": ["play"]}
    if (state.get("ready_state") or 0) < 3 and not state.get("current_time"):
        return {"phase": "loading", **PHASES["loading"], "guide": "", "actions": []}
    if (state.get("ready_state") or 0) < 3:
        return {"phase": "buffering", **PHASES["buffering"], "guide": "Waiting for data from YouTube.", "actions": []}
    return {"phase": "playing", **PHASES["playing"], "guide": "", "actions": ["pause"]}


# ---------------------------------------------------------------- watcher

async def read_state() -> dict:
    try:
        res = await cdp.evaluate(cdp.JS_STATE)
    except cdp.CDPError as exc:
        return {"available": False, "reason": str(exc)}
    st = res.get("value") or {}
    st["available"] = True
    return st


def _mark_once(kind: str, key: str) -> bool:
    k = (kind, key)
    now = time.time()
    if now - _auto_marks.get(k, 0) < 20:
        return False
    _auto_marks[k] = now
    return True


async def tick(play_link) -> None:
    """One watcher pass. `play_link(link)` is main.py's play routine
    (async) so auto-advance goes through exactly the same path as a
    tap in the library."""
    from . import control
    try:
        cur = await asyncio.get_event_loop().run_in_executor(None, control.read_current_source)
    except Exception:  # noqa: BLE001
        return
    if cur.get("SOURCE_TYPE") != "webpage":
        return
    st = await read_state()
    last["state"] = st; last["at"] = time.time()
    if not st.get("available"):
        return
    s = get_settings()
    auto = []
    if s["yt_auto_dismiss_prompts"] and st.get("continue_prompt"):
        try:
            r = await cdp.evaluate(cdp.JS_DISMISS_PROMPT, user_gesture=True)
            if r.get("value") and _mark_once("dismiss", st.get("url", "")):
                db.audit(None, "youtube_auto_dismiss_prompt", (st.get("title") or "")[:80]); auto.append("dismissed prompt")
        except cdp.CDPError:
            pass
    if s["yt_auto_skip_ads"] and st.get("ad_showing") and st.get("skippable"):
        try:
            r = await cdp.evaluate(cdp.JS_SKIP_AD, user_gesture=True)
            if r.get("value") and _mark_once("skip", st.get("url", "") + str(int(time.time() // 30))):
                db.audit(None, "youtube_auto_skip_ad", (st.get("title") or "")[:80]); auto.append("skipped ad")
        except cdp.CDPError:
            pass
    # Keep the player filling the canvas: a watch page shows YouTube's
    # masthead/sidebar unless its player is fullscreen, and YouTube drops
    # fullscreen on some transitions (ad -> video, playlist advance, an
    # upsell card, ...). Retry every tick like dismiss-prompt/skip-ad do -
    # _mark_once only dedupes the audit/toast, never the click itself -
    # because a dropped-fullscreen tick shows real black letterboxing to
    # viewers; gating the retry behind the 20s dedup (as this used to)
    # left it on screen far longer than the ~4s tick interval should allow.
    if s["yt_keep_fullscreen"] and st.get("has_video") and not st.get("fullscreen") and not st.get("continue_prompt") \
            and not st.get("ended") and st.get("body_hint", "") == "":
        try:
            r = await cdp.evaluate(cdp.JS_FULLSCREEN, user_gesture=True)
            if r.get("value") in ("clicked-yt-button", "requested") and _mark_once("fullscreen", st.get("url", "")):
                auto.append("fullscreen restored")
        except cdp.CDPError:
            pass
    # A saved link's speed: YouTube resets playbackRate after an ad or a
    # playlist step, so put it back while the video (not an ad) plays.
    if current["link_id"] is not None and st.get("has_video") and not st.get("ad_showing") and not st.get("paused"):
        link = next((l for l in list_links() if l["id"] == current["link_id"]), None)
        want = float((link or {}).get("options", {}).get("speed") or 1.0)
        if link and abs(float(st.get("speed") or 1.0) - want) > 0.01 and _mark_once("speed", st.get("url", "")):
            try:
                await cdp.evaluate(cdp.js_set_speed(want)); auto.append(f"speed {want}x")
            except cdp.CDPError:
                pass
    if s["yt_auto_advance"] and st.get("ended") and st.get("has_video") and current["link_id"] is not None:
        nxt = neighbour(+1)
        if nxt and _mark_once("advance", f"{current['link_id']}->{nxt['id']}@{int(st.get('duration') or 0)}"):
            try:
                await play_link(nxt)
                db.audit(None, "youtube_auto_advance", f"{current['link_id']} -> {nxt['id']} {nxt['name'][:60]}"); auto.append("advanced to " + nxt["name"][:40])
            except Exception as exc:  # noqa: BLE001
                auto.append("auto-advance failed: " + str(exc)[:80])
    if auto:
        last["auto"] = (auto + last["auto"])[:5]
        last["auto_at"] = time.time()
