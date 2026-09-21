"""OBS-Style Text Overlay Engine.
Provides 0% CPU text overlay rendering directly onto Display :99 Chrome kiosk
via hardware-accelerated CSS/DOM injection over the Chrome DevTools Protocol.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import cdp, config

logger = logging.getLogger("zoom-stream.overlay")

GOOGLE_FONTS = [
    "Roboto",
    "Montserrat",
    "Poppins",
    "Bebas Neue",
    "Oswald",
    "Inter",
    "Anton",
    "Playfair Display",
    "Open Sans",
    "Lato",
    "Raleway",
    "Source Sans Pro",
    "Fira Sans",
    "Merriweather",
    "Cinzel",
    "Ubuntu",
    "Bangers",
]

DEFAULT_OVERLAY_STATE = {
    "text": "LIVE BROADCAST",
    "font_family": "Montserrat",
    "font_size": 42,
    "font_color": "#FFFFFF",
    "font_opacity": 100,
    "outline_enabled": True,
    "outline_color": "#000000",
    "outline_width": 3,
    "box_enabled": True,
    "box_color": "#000000",
    "box_opacity": 75,
    "box_padding": 16,
    "box_radius": 8,
    "shadow_enabled": True,
    "shadow_color": "#000000",
    "shadow_blur": 10,
    "shadow_x": 2,
    "shadow_y": 4,
    "pos_x": 5.0,
    "pos_y": 88.0,
    "anchor": "bottom-left",
    "visible": False,
}


def hex_to_rgba(hex_color: str, opacity_pct: int | float = 100) -> str:
    """Converts HEX string to CSS rgba(r, g, b, a)."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "000000"
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except ValueError:
        r, g, b = 0, 0, 0
    alpha = max(0.0, min(1.0, float(opacity_pct) / 100.0))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def get_overlay_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.OVERLAY_CONFIG_FILE


def get_overlay_state() -> dict:
    """Loads current overlay configuration from disk or returns defaults."""
    p = get_overlay_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_OVERLAY_STATE)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_OVERLAY_STATE)


def save_overlay_state(updates: dict) -> dict:
    """Updates overlay configuration on disk and returns updated state."""
    current = get_overlay_state()
    for k, v in updates.items():
        if k in DEFAULT_OVERLAY_STATE:
            # Type coerce numbers
            if isinstance(DEFAULT_OVERLAY_STATE[k], (int, float)) and not isinstance(DEFAULT_OVERLAY_STATE[k], bool):
                try:
                    current[k] = float(v) if isinstance(DEFAULT_OVERLAY_STATE[k], float) else int(v)
                except (ValueError, TypeError):
                    continue
            elif isinstance(DEFAULT_OVERLAY_STATE[k], bool):
                current[k] = bool(v)
            else:
                current[k] = str(v)

    p = get_overlay_path()
    try:
        p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to write overlay config to %s: %s", p, exc)

    return current


def toggle_overlay_visibility() -> dict:
    """Toggles master SHOW/HIDE overlay flag."""
    current = get_overlay_state()
    return save_overlay_state({"visible": not current.get("visible", False)})


def generate_overlay_js(state: dict) -> str:
    """Generates the client-side JavaScript snippet to inject/update the overlay in Chrome kiosk."""
    visible = state.get("visible", False)
    text = (state.get("text") or "").replace("\n", "<br>")
    font_family = state.get("font_family") or "Montserrat"
    font_size = state.get("font_size", 42)
    font_color_rgba = hex_to_rgba(state.get("font_color", "#FFFFFF"), state.get("font_opacity", 100))

    outline_enabled = state.get("outline_enabled", False)
    outline_color = state.get("outline_color", "#000000")
    outline_width = state.get("outline_width", 2)
    outline_css = f"-webkit-text-stroke: {outline_width}px {outline_color};" if outline_enabled else "-webkit-text-stroke: 0;"

    box_enabled = state.get("box_enabled", False)
    box_color_rgba = hex_to_rgba(state.get("box_color", "#000000"), state.get("box_opacity", 75)) if box_enabled else "transparent"
    box_padding = state.get("box_padding", 16) if box_enabled else 0
    box_radius = state.get("box_radius", 8) if box_enabled else 0

    shadow_enabled = state.get("shadow_enabled", False)
    shadow_color = state.get("shadow_color", "#000000")
    shadow_blur = state.get("shadow_blur", 10)
    shadow_x = state.get("shadow_x", 2)
    shadow_y = state.get("shadow_y", 4)
    shadow_css = f"text-shadow: {shadow_x}px {shadow_y}px {shadow_blur}px {shadow_color};" if shadow_enabled else "text-shadow: none;"

    pos_x = state.get("pos_x", 5.0)
    pos_y = state.get("pos_y", 88.0)
    anchor = state.get("anchor", "custom")

    # Determine transform alignment based on anchor or relative position
    if anchor == "top-left":
        transform = "translate(0, 0)"
    elif anchor == "top-right":
        transform = "translate(-100%, 0)"
    elif anchor == "bottom-left":
        transform = "translate(0, -100%)"
    elif anchor == "bottom-right":
        transform = "translate(-100%, -100%)"
    elif anchor == "center":
        transform = "translate(-50%, -50%)"
    else:
        # Custom anchor based on percentage coordinates
        tx = "-100%" if pos_x > 70 else ("-50%" if pos_x > 30 else "0")
        ty = "-100%" if pos_y > 70 else ("-50%" if pos_y > 30 else "0")
        transform = f"translate({tx}, {ty})"

    font_url_family = font_family.replace(" ", "+")

    js = f"""(() => {{
      // 1. Ensure Google Font is loaded
      const fontId = 'font-overlay-{font_url_family.lower()}';
      if (!document.getElementById(fontId)) {{
        const link = document.createElement('link');
        link.id = fontId;
        link.rel = 'stylesheet';
        link.href = 'https://fonts.googleapis.com/css2?family={font_url_family}:wght@400;600;700;800&display=swap';
        document.head.appendChild(link);
      }}

      // 2. Locate or create overlay container
      let overlay = document.getElementById('obs-text-overlay');
      if (!overlay) {{
        overlay = document.createElement('div');
        overlay.id = 'obs-text-overlay';
        overlay.style.position = 'fixed';
        overlay.style.zIndex = '2147483646';
        overlay.style.pointerEvents = 'none';
        overlay.style.willChange = 'transform, top, left';
        overlay.style.lineHeight = '1.25';
        overlay.style.maxWidth = '90vw';
        overlay.style.wordBreak = 'break-word';
        (document.body || document.documentElement).appendChild(overlay);
      }}

      // 3. Apply styles & visibility
      overlay.style.display = {'block' if visible else 'none'};
      if (!{json.dumps(visible)}) return true;

      overlay.style.left = '{pos_x}%';
      overlay.style.top = '{pos_y}%';
      overlay.style.transform = '{transform}';
      overlay.style.fontFamily = '"{font_family}", sans-serif';
      overlay.style.fontSize = '{font_size}px';
      overlay.style.color = '{font_color_rgba}';
      overlay.style.backgroundColor = '{box_color_rgba}';
      overlay.style.padding = '{box_padding}px';
      overlay.style.borderRadius = '{box_radius}px';
      overlay.style.cssText += '; {outline_css} {shadow_css}';
      overlay.innerHTML = {json.dumps(text)};
      return true;
    }})()"""
    return js


async def push_overlay_to_kiosk(state: dict | None = None) -> bool:
    """Pushes current overlay state directly into Chrome kiosk via CDP.
    Returns True if evaluated successfully, False if DevTools not connected.
    """
    if state is None:
        state = get_overlay_state()
    js = generate_overlay_js(state)
    try:
        await cdp.evaluate(js)
        return True
    except Exception as exc:
        logger.debug("Overlay push to kiosk skipped (kiosk not running or DevTools busy): %s", exc)
        return False
