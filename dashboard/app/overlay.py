"""OBS-Style Text Overlay Engine.
Provides 0% CPU text overlay rendering directly onto Display :99 Chrome kiosk
via hardware-accelerated CSS/DOM injection over the Chrome DevTools Protocol.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re

from . import cdp, config

logger = logging.getLogger("zoom-stream.overlay")

GOOGLE_FONT_CATEGORIES = {
    "High-Impact & Broadcast Titles": [
        "Anton",
        "Bebas Neue",
        "Teko",
        "Archivo Black",
        "Russo One",
        "Righteous",
        "Bungee",
        "Alfa Slab One",
        "Black Han Sans",
        "Titan One",
        "Bangers",
        "Squada One",
    ],
    "Modern & Clean Sans-Serif": [
        "Montserrat",
        "Poppins",
        "Roboto",
        "Inter",
        "Open Sans",
        "Lato",
        "Raleway",
        "DM Sans",
        "Plus Jakarta Sans",
        "Work Sans",
        "Outfit",
        "Rubik",
        "Nunito",
        "Kanit",
    ],
    "Condensed & Tall": [
        "Oswald",
        "Barlow Semi Condensed",
        "Fjalla One",
        "Pathway Gothic One",
        "Saira Condensed",
        "Antonio",
        "Six Caps",
        "Yanone Kaffeesatz",
    ],
    "Tech, Sci-Fi & Gaming": [
        "Orbitron",
        "Exo 2",
        "Audiowide",
        "Rajdhani",
        "Chakra Petch",
        "Michroma",
        "Oxanium",
        "Share Tech Mono",
        "Press Start 2P",
        "Silkscreen",
    ],
    "Elegant & Editorial Serif": [
        "Playfair Display",
        "Merriweather",
        "Cinzel",
        "Lora",
        "Cormorant Garamond",
        "Bodoni Moda",
        "Spectral",
        "Prata",
        "DM Serif Display",
        "Abril Fatface",
    ],
    "Handwritten & Script": [
        "Pacifico",
        "Caveat",
        "Dancing Script",
        "Permanent Marker",
        "Great Vibes",
        "Satisfy",
        "Shadows Into Light",
        "Kalam",
    ],
    "Sri Lankan / Sinhala Unicode Support": [
        "Noto Sans Sinhala",
        "Noto Serif Sinhala",
        "Abhaya Libre",
    ],
}

GOOGLE_FONTS = [font for fonts in GOOGLE_FONT_CATEGORIES.values() for font in fonts]


def get_google_fonts_preview_urls() -> list[str]:
    """Returns batched Google Fonts CSS2 URLs to efficiently preload preview fonts."""
    batches = []
    chunk = []
    for f in GOOGLE_FONTS:
        chunk.append(f.replace(" ", "+"))
        if len(chunk) >= 15:
            batches.append(
                "https://fonts.googleapis.com/css2?"
                + "&".join(f"family={name}" for name in chunk)
                + "&display=swap"
            )
            chunk = []
    if chunk:
        batches.append(
            "https://fonts.googleapis.com/css2?"
            + "&".join(f"family={name}" for name in chunk)
            + "&display=swap"
        )
    return batches

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

    # --- Encoder-burned watermark (Part 4 fix) ---
    # The fields above drive the live CDP/DOM preview only - see
    # push_overlay_to_kiosk()'s docstring for why that was never actually
    # visible in the broadcast. These fields drive the real FFmpeg
    # drawtext/overlay filter built by scripts/lib.sh's
    # build_watermark_filter() and are what the operator is actually
    # toggling. "anchor" above is reused for encoder positioning too (now
    # a full 9-point grid, not just 5) - margin_x/margin_y are pixels from
    # the anchored edge, independent of the preview's pos_x/pos_y percentages.
    "mode": "text",  # "text" | "image"
    "encoder_font": "inter",  # "inter" | "jetbrains-mono" - see scripts/fonts/
    "margin_x": 24,
    "margin_y": 24,
    "image_path": "",
    "image_opacity": 100,
    "image_scale_pct": 15.0,  # width, as a percentage of the video width
}

ENCODER_ANCHORS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)
ENCODER_FONTS = {"inter": "Inter-Variable.ttf", "jetbrains-mono": "JetBrainsMono.ttf"}


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
    visible = bool(state.get("visible", False))
    text = (state.get("text") or "").replace("\n", "<br>")
    font_family = state.get("font_family") or "Montserrat"
    font_size = state.get("font_size", 42)
    font_color_rgba = hex_to_rgba(state.get("font_color", "#FFFFFF"), state.get("font_opacity", 100))

    outline_enabled = state.get("outline_enabled", False)
    outline_color = state.get("outline_color", "#000000")
    outline_width = state.get("outline_width", 2)
    outline_css = f"-webkit-text-stroke: {outline_width}px {outline_color} !important;" if outline_enabled else "-webkit-text-stroke: 0 !important;"

    box_enabled = state.get("box_enabled", False)
    box_color_rgba = hex_to_rgba(state.get("box_color", "#000000"), state.get("box_opacity", 75)) if box_enabled else "transparent"
    box_padding = state.get("box_padding", 16) if box_enabled else 0
    box_radius = state.get("box_radius", 8) if box_enabled else 0

    shadow_enabled = state.get("shadow_enabled", False)
    shadow_color = state.get("shadow_color", "#000000")
    shadow_blur = state.get("shadow_blur", 10)
    shadow_x = state.get("shadow_x", 2)
    shadow_y = state.get("shadow_y", 4)
    shadow_css = f"text-shadow: {shadow_x}px {shadow_y}px {shadow_blur}px {shadow_color} !important;" if shadow_enabled else "text-shadow: none !important;"

    pos_x = state.get("pos_x", 5.0)
    pos_y = state.get("pos_y", 88.0)
    anchor = state.get("anchor", "custom")

    # Determine transform alignment based on anchor or relative position -
    # the full 9-point grid (see overlay.py's ENCODER_ANCHORS), so the
    # preview at least visually matches the shape of what the encoder-side
    # filter (scripts/lib.sh's build_watermark_filter()) actually renders,
    # even though this DOM preview and the real x11grab-captured output
    # are fundamentally different rendering surfaces - see
    # push_overlay_to_kiosk()'s docstring.
    if anchor == "top-left":
        transform = "translate(0, 0)"
    elif anchor == "top-center":
        transform = "translate(-50%, 0)"
    elif anchor == "top-right":
        transform = "translate(-100%, 0)"
    elif anchor == "middle-left":
        transform = "translate(0, -50%)"
    elif anchor == "middle-right":
        transform = "translate(-100%, -50%)"
    elif anchor == "bottom-left":
        transform = "translate(0, -100%)"
    elif anchor == "bottom-center":
        transform = "translate(-50%, -100%)"
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
    font_slug = re.sub(r"[^a-z0-9]+", "-", font_family.lower()).strip("-")
    font_id = f"font-overlay-{font_slug}"
    display_val = "block" if visible else "none"
    visibility_val = "visible" if visible else "hidden"
    opacity_val = "1" if visible else "0"

    js = f"""(() => {{
      // 1. Ensure Google Font is loaded dynamically
      const fontUrlFamily = {json.dumps(font_url_family)};
      const fontId = {json.dumps(font_id)};
      if (!document.getElementById(fontId)) {{
        const link = document.createElement('link');
        link.id = fontId;
        link.rel = 'stylesheet';
        link.href = 'https://fonts.googleapis.com/css2?family=' + fontUrlFamily + '&display=swap';
        document.head.appendChild(link);
      }}

      // 2. Clean up legacy obs-text-overlay if present outside our root container
      const legacy = document.getElementById('obs-text-overlay');
      if (legacy) {{
        legacy.remove();
      }}

      // 3. Locate or create root watermark container attached to document.documentElement
      let container = document.getElementById('livestream-watermark-overlay');
      if (!container) {{
        container = document.createElement('div');
        container.id = 'livestream-watermark-overlay';
        (document.documentElement || document.body).appendChild(container);
      }}

      // Ensure container is child of document.documentElement (top-most DOM root)
      if (container.parentElement !== document.documentElement && document.documentElement) {{
        document.documentElement.appendChild(container);
      }}

      const isVisible = {json.dumps(visible)};
      container.style.cssText = 'position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; pointer-events: none !important; z-index: 2147483647 !important; overflow: hidden !important; margin: 0 !important; padding: 0 !important; border: none !important; display: {display_val} !important; visibility: {visibility_val} !important; opacity: {opacity_val} !important;';

      if (!isVisible) {{
        return {{ success: true, visible: false, attached: true }};
      }}

      // 4. Locate or create inner overlay box
      let inner = document.getElementById('livestream-watermark-inner');
      if (!inner) {{
        inner = document.createElement('div');
        inner.id = 'livestream-watermark-inner';
        container.appendChild(inner);
      }}

      inner.style.cssText = 'position: absolute !important; pointer-events: none !important; will-change: transform, top, left !important; line-height: 1.25 !important; max-width: 90vw !important; word-break: break-word !important; white-space: pre-wrap !important; box-sizing: border-box !important; left: {pos_x}% !important; top: {pos_y}% !important; transform: {transform} !important; font-family: "{font_family}", sans-serif !important; font-size: {font_size}px !important; color: {font_color_rgba} !important; background-color: {box_color_rgba} !important; padding: {box_padding}px !important; border-radius: {box_radius}px !important; {outline_css} {shadow_css}';
      inner.innerHTML = {json.dumps(text)};

      // 5. Persistent MutationObserver to guard against React re-renders wiping DOM elements
      if (!window.__zoom_watermark_observer) {{
        try {{
          const rootNode = document.documentElement || document.body;
          window.__zoom_watermark_observer = new MutationObserver(() => {{
            const c = document.getElementById('livestream-watermark-overlay');
            if (!c || c.parentElement !== document.documentElement) {{
              if (c) c.remove();
              if (typeof window.__zoom_reinject_watermark === 'function') {{
                window.__zoom_reinject_watermark();
              }}
            }}
          }});
          window.__zoom_watermark_observer.observe(rootNode, {{ childList: true, subtree: false }});
        }} catch (e) {{
          console.warn('Watermark MutationObserver error:', e);
        }}
      }}

      window.__zoom_reinject_watermark = () => {{
        let c = document.getElementById('livestream-watermark-overlay');
        if (!c && document.documentElement) {{
          document.documentElement.appendChild(container);
        }}
      }};

      return {{
        success: true,
        visible: true,
        attached: Boolean(document.getElementById('livestream-watermark-overlay')),
        parent: container.parentElement ? container.parentElement.tagName : null
      }};
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


async def get_overlay_kiosk_status() -> dict:
    """Probes Chrome kiosk via CDP to determine if overlay is injected and visible on Display :99."""
    state = get_overlay_state()
    try:
        target = await cdp._get_page_target()
        js_probe = """
        (() => {
          const container = document.getElementById('livestream-watermark-overlay');
          const inner = document.getElementById('livestream-watermark-inner');
          return {
            injected: Boolean(container && inner),
            visible: Boolean(container && container.style.display !== 'none' && container.style.visibility !== 'hidden'),
            parent: container && container.parentElement ? container.parentElement.tagName : null,
            text: inner ? (inner.innerText || '') : ''
          };
        })()
        """
        res = await cdp.evaluate(js_probe)
        val = res.get("value") or {}
        return {
            "connected": True,
            "target_title": target.get("title", ""),
            "target_url": target.get("url", ""),
            "configured_visible": state.get("visible", False),
            "injected": bool(val.get("injected", False)),
            "visible": bool(val.get("visible", False)),
            "parent": val.get("parent"),
            "text": val.get("text", ""),
        }
    except Exception as exc:
        return {
            "connected": False,
            "target_title": "",
            "target_url": "",
            "configured_visible": state.get("visible", False),
            "injected": False,
            "visible": False,
            "error": str(exc),
        }


async def reinject_overlay() -> dict:
    """Forces immediate re-injection of overlay state into Chrome kiosk via CDP."""
    state = get_overlay_state()
    success = await push_overlay_to_kiosk(state)
    status = await get_overlay_kiosk_status()
    status["pushed"] = success
    return status
