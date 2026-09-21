"""Emergency Failover & Holding Card Slate ("Be Right Back" / BRB).
Displays a branded "Stream Will Resume Shortly" holding card on Display :99
Chrome kiosk and X11 display when Zoom disconnects or when triggered manually.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import cdp, config

logger = logging.getLogger("zoom-stream.slate")

DEFAULT_SLATE_STATE = {
    "active": False,
    "title": "Stream Will Resume Shortly",
    "subtitle": "Please stand by • We'll be right back",
    "updated_at": 0.0,
}


def get_slate_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.BRB_SLATE_FILE


def get_brb_state() -> dict:
    p = get_slate_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_SLATE_STATE)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_SLATE_STATE)


def set_brb_state(active: bool, title: str | None = None, subtitle: str | None = None) -> dict:
    current = get_brb_state()
    current["active"] = bool(active)
    if title is not None:
        current["title"] = str(title)
    if subtitle is not None:
        current["subtitle"] = str(subtitle)
    current["updated_at"] = time.time()

    p = get_slate_path()
    try:
        p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to write BRB slate state to %s: %s", p, exc)

    return current


def toggle_brb_state() -> dict:
    cur = get_brb_state()
    return set_brb_state(not cur.get("active", False))


def generate_brb_js(state: dict) -> str:
    active = state.get("active", False)
    title = state.get("title", "Stream Will Resume Shortly")
    subtitle = state.get("subtitle", "Please stand by • We'll be right back")

    return f"""(() => {{
      let slate = document.getElementById('stream-brb-holding-card');
      if (!slate) {{
        slate = document.createElement('div');
        slate.id = 'stream-brb-holding-card';
        slate.style.position = 'fixed';
        slate.style.inset = '0';
        slate.style.width = '100vw';
        slate.style.height = '100vh';
        slate.style.background = 'radial-gradient(circle at center, #131b2e 0%, #070a0f 100%)';
        slate.style.zIndex = '2147483647';
        slate.style.display = 'flex';
        slate.style.flexDirection = 'column';
        slate.style.alignItems = 'center';
        slate.style.justifyContent = 'center';
        slate.style.fontFamily = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
        slate.style.color = '#ffffff';
        slate.style.textAlign = 'center';
        slate.style.padding = '2rem';
        slate.style.boxSizing = 'border-box';
        slate.style.pointerEvents = 'auto';

        slate.innerHTML = `
          <div style="display:inline-flex; align-items:center; gap:10px; background:rgba(239,68,68,0.15); border:1px solid rgba(239,68,68,0.4); padding:6px 18px; border-radius:9999px; margin-bottom:28px;">
            <span style="width:12px; height:12px; border-radius:50%; background:#ef4444; box-shadow:0 0 12px #ef4444;"></span>
            <span style="font-size:14px; font-weight:700; letter-spacing:2.5px; color:#fca5a5; text-transform:uppercase;">Stream Paused</span>
          </div>
          <h1 id="brb-slate-title" style="font-size:clamp(32px, 5vw, 64px); font-weight:800; letter-spacing:-1px; margin:0 0 16px 0; text-shadow:0 4px 24px rgba(0,0,0,0.7); max-width:850px; line-height:1.15;"></h1>
          <p id="brb-slate-sub" style="font-size:clamp(16px, 2.5vw, 24px); color:#94a3b8; margin:0; font-weight:400; max-width:650px; line-height:1.4;"></p>
        `;
        (document.body || document.documentElement).appendChild(slate);
      }}

      const titleEl = document.getElementById('brb-slate-title');
      const subEl = document.getElementById('brb-slate-sub');
      if (titleEl) titleEl.innerText = {json.dumps(title)};
      if (subEl) subEl.innerText = {json.dumps(subtitle)};

      slate.style.display = {json.dumps('flex' if active else 'none')};
      slate.style.visibility = {json.dumps('visible' if active else 'hidden')};
      slate.style.opacity = {json.dumps('1' if active else '0')};
      return true;
    }})()"""


async def push_brb_to_kiosk(state: dict | None = None) -> bool:
    if state is None:
        state = get_brb_state()
    js = generate_brb_js(state)
    try:
        await cdp.evaluate(js)
        return True
    except Exception as exc:
        logger.debug("BRB slate push to kiosk skipped: %s", exc)
        return False
