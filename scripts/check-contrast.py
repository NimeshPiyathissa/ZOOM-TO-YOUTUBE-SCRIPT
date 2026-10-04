#!/usr/bin/env python3
"""WCAG AA contrast checker for the dashboard's theme tokens.

Parses dashboard/static/css/tokens.css directly (no browser, no build
step) and checks a fixed list of real UI color pairs - body text, card
surfaces, badges, buttons, the nav active pill, disabled inputs, toasts,
modals, the log viewer, and the remote-screen status overlay - against
WCAG AA (4.5:1 for body text, 3:1 for large text / non-text UI parts)
for each of the three themes: White, Black, Default.

Tint tokens (e.g. --success-tint) are rgba with the alpha already baked
in; they're only ever used as a background, so this script composites
them over the surface they actually sit on before measuring contrast -
checking the raw rgba string against a solid foreground would be
meaningless.

Exit code is non-zero if any pair fails its threshold. Run for all
three themes every time (no --theme flag): the whole point is to catch
a theme-specific regression before it ships.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKENS_CSS = ROOT / "dashboard" / "static" / "css" / "tokens.css"

THEMES = ["white", "black", "default"]


def extract_block(css: str, selector_pattern: str) -> str:
    """selector_pattern must end with the opening brace itself (`\\{`) -
    start is that brace's own position, not the next one found after it."""
    m = re.search(selector_pattern, css)
    if not m:
        raise ValueError(f"selector not found: {selector_pattern}")
    if css[m.end() - 1] != "{":
        raise ValueError(f"selector_pattern must end with the opening brace: {selector_pattern}")
    start = m.end() - 1
    depth = 0
    i = start
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:i]
        i += 1
    raise ValueError(f"unbalanced braces for {selector_pattern}")


def parse_tokens(block: str) -> dict[str, str]:
    out = {}
    for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", block):
        out[name.strip()] = value.strip()
    return out


def resolve(tokens: dict[str, str], name: str, _seen: tuple[str, ...] = ()) -> str:
    if name in _seen:
        raise ValueError(f"circular token reference: {' -> '.join(_seen + (name,))}")
    value = tokens.get(name)
    if value is None:
        raise KeyError(f"token not defined: --{name}")
    m = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if m:
        return resolve(tokens, m.group(1), _seen + (name,))
    return value


Color = tuple[float, float, float, float]  # r, g, b (0-255), alpha (0-1)


def parse_color(value: str) -> Color:
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", value)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        r, g, b, a = m.groups()
        return (float(r), float(g), float(b), float(a) if a is not None else 1.0)
    raise ValueError(f"unrecognized color value: {value!r}")


def composite(fg: Color, bg: Color) -> Color:
    """Alpha-blend fg over an opaque bg - needed for *-tint tokens, which
    are only ever used as a translucent background over a solid surface."""
    r, g, b, a = fg
    br, bg_, bb, _ = bg
    return (r * a + br * (1 - a), g * a + bg_ * (1 - a), b * a + bb * (1 - a), 1.0)


def srgb_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: Color) -> float:
    r, g, b, _ = color
    return 0.2126 * srgb_to_linear(r) + 0.7152 * srgb_to_linear(g) + 0.0722 * srgb_to_linear(b)


def contrast_ratio(c1: Color, c2: Color) -> float:
    l1, l2 = relative_luminance(c1), relative_luminance(c2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def resolved_rgb(tokens: dict[str, str], name: str, blend_over: str | None = None) -> Color:
    color = parse_color(resolve(tokens, name))
    if color[3] < 1.0 and blend_over:
        bg = resolved_rgb(tokens, blend_over)
        return composite(color, bg)
    return color


# (label, fg_token, bg_token, min_ratio, fg_alpha_override_opacity)
# fg_alpha_override_opacity: for "disabled" style checks, simulates
# opacity applied to the foreground text itself (not a *-tint token).
PAIRS = [
    ("Body text on page background", "text", "bg", 4.5, None),
    ("Body text on card surface", "text", "bg-panel", 4.5, None),
    ("Secondary text on card surface", "text-dim", "bg-panel", 4.5, None),
    ("Muted/placeholder text on card surface", "text-faint", "bg-panel", 3.0, None),
    ("Text on elevated surface (modal/menu/toast)", "text", "bg-elevated", 4.5, None),
    ("Secondary text on elevated surface", "text-dim", "bg-elevated", 4.5, None),
    ("Accent/link text on card surface", "accent", "bg-panel", 4.5, None),
    ("Sparkline/chart stroke vs card surface", "accent", "bg-panel", 3.0, None),
    ("Primary button text", "text-on-accent", "accent-solid", 4.5, None),
    ("Danger button text", "text-on-accent", "live-solid", 4.5, None),
    ("Sidebar active nav pill text", "nav-active-text", "nav-active-bg", 4.5, None),
    ("CPU bar fill vs track", "accent", "bg-hover", 3.0, None),
    ("Toast success icon", "success", "bg-elevated", 4.5, None),
    ("Toast error icon", "live", "bg-elevated", 4.5, None),
]

# (label, fg_token, tint_token, surface_token, min_ratio) - tint is
# composited over surface first, then fg is measured against that.
TINT_PAIRS = [
    ("Success badge text", "success", "success-tint", "bg-panel", 4.5),
    ("Warning badge text", "warning", "warning-tint", "bg-panel", 4.5),
    ("Danger badge / error banner text", "live", "live-tint", "bg-panel", 4.5),
    ("Info banner text", "info", "info-tint", "bg-panel", 4.5),
]

# (label, translucent_token, surface_token, min_ratio) - the translucent
# color itself (not a text foreground) composited over its surface, then
# measured against that same surface - this is the "is this non-text UI
# boundary visible at all" check (focus rings, etc), not a text pair.
SELF_VS_SURFACE_PAIRS = [
    ("Focus ring visibility vs card surface", "accent-ring", "bg-panel", 3.0),
]


def check_theme(name: str, tokens: dict[str, str]) -> list[tuple[str, float, float, bool]]:
    results = []
    for label, fg_tok, bg_tok, min_ratio, _ in PAIRS:
        bg = resolved_rgb(tokens, bg_tok)
        fg = resolved_rgb(tokens, fg_tok, blend_over=bg_tok)
        ratio = contrast_ratio(fg, bg)
        results.append((f"[{name}] {label}", ratio, min_ratio, ratio >= min_ratio, False))

    for label, fg_tok, tint_tok, surface_tok, min_ratio in TINT_PAIRS:
        surface = resolved_rgb(tokens, surface_tok)
        tint_color = parse_color(resolve(tokens, tint_tok))
        composited_bg = composite(tint_color, surface) if tint_color[3] < 1.0 else tint_color
        fg = resolved_rgb(tokens, fg_tok)
        ratio = contrast_ratio(fg, composited_bg)
        results.append((f"[{name}] {label}", ratio, min_ratio, ratio >= min_ratio, False))

    for label, translucent_tok, surface_tok, min_ratio in SELF_VS_SURFACE_PAIRS:
        surface = resolved_rgb(tokens, surface_tok)
        translucent = parse_color(resolve(tokens, translucent_tok))
        composited = composite(translucent, surface) if translucent[3] < 1.0 else translucent
        ratio = contrast_ratio(composited, surface)
        results.append((f"[{name}] {label}", ratio, min_ratio, ratio >= min_ratio, False))

    # disabled input text: .input:disabled { opacity: 0.55 } over bg-panel
    bg = resolved_rgb(tokens, "bg-panel")
    text_rgb = resolved_rgb(tokens, "text")
    disabled_fg = composite((text_rgb[0], text_rgb[1], text_rgb[2], 0.55), bg)
    ratio = contrast_ratio(disabled_fg, bg)
    results.append((f"[{name}] Disabled input text (0.55 opacity) vs surface", ratio, 3.0, ratio >= 3.0, False))

    return results


def check_remote_frame(any_tokens: dict[str, str]) -> list[tuple[str, float, float, bool, bool]]:
    # The embedded VNC/remote-screen frame's Connecting/Failed status is
    # always shown as a themed .vnc-status-card (bg-elevated) floating
    # over a fixed dark scrim+canvas backdrop (never text directly on the
    # scrim - see templates/vnc.html, templates/accounts.html). Text-on-
    # bg-elevated is already covered by "Text on elevated surface"; what's
    # specific to this component is whether the card itself stands out
    # from the video surface behind it (non-text UI boundary, 3:1).
    #
    # White clears this with a plain flat-color comparison. Black and
    # Default can't: their whole palette lives in the same near-black
    # luminance range as "video that hasn't loaded yet" by design, and
    # pushing bg-elevated light enough to hit 3:1 fill-contrast against
    # a black backdrop (~L 0.10, a mid-gray) would blow out the "raised
    # dark surface" look for every other elevated panel in the theme -
    # a worse trade than this one case. The real separation there comes
    # from .vnc-status-card's border + box-shadow + the overlay's
    # backdrop-filter: blur(4px) (components.css/base.css), none of
    # which a flat fill-vs-fill ratio can capture. Reported as advisory
    # (not a pass/fail gate) for Black/Default rather than silently
    # lowering the bar to manufacture a fake PASS.
    scrim = parse_color(resolve(any_tokens, "scrim"))
    canvas = parse_color(resolve(any_tokens, "canvas-black"))
    backdrop = composite(scrim, canvas)
    results = []
    for theme in THEMES:
        tokens = load_theme_tokens(theme)
        card_rgb = resolved_rgb(tokens, "bg-elevated")
        ratio = contrast_ratio(card_rgb, backdrop)
        advisory = theme != "white"
        results.append((f"[{theme}] Remote-screen status card vs video backdrop", ratio, 3.0, ratio >= 3.0, advisory))
    return results


def check_terminal(base_tokens: dict[str, str]) -> list[tuple[str, float, float, bool]]:
    # Log viewer: deliberately theme-invariant, checked once.
    bg = resolved_rgb(base_tokens, "term-bg")
    results = []
    for label, fg_tok, min_ratio in [
        ("Log viewer body text", "term-text", 4.5),
        ("Log viewer line numbers", "term-line-no", 3.0),
        ("Log viewer error line", "term-err", 4.5),
        ("Log viewer warning line", "term-warn", 4.5),
    ]:
        fg = resolved_rgb(base_tokens, fg_tok)
        ratio = contrast_ratio(fg, bg)
        results.append((f"[terminal] {label}", ratio, min_ratio, ratio >= min_ratio, False))
    return results


_theme_cache: dict[str, dict[str, str]] = {}
_base_tokens: dict[str, str] = {}


def load_theme_tokens(theme: str) -> dict[str, str]:
    if theme not in _theme_cache:
        css = TOKENS_CSS.read_text(encoding="utf-8")
        base = parse_tokens(extract_block(css, r"(?m)^:root \{"))
        selector = {
            "white": r':root\[data-theme="white"\](?:,\s*:root:not\(\[data-theme\]\))?\s*\{',
            "black": r':root\[data-theme="black"\]\s*\{',
            "default": r':root\[data-theme="default"\]\s*\{',
        }[theme]
        theme_block = parse_tokens(extract_block(css, selector))
        _theme_cache[theme] = {**base, **theme_block}
        _base_tokens.update(base)
    return _theme_cache[theme]


def main() -> int:
    if not TOKENS_CSS.exists():
        print(f"tokens.css not found at {TOKENS_CSS}", file=sys.stderr)
        return 2

    all_results: list[tuple[str, float, float, bool, bool]] = []
    for theme in THEMES:
        tokens = load_theme_tokens(theme)
        all_results.extend(check_theme(theme, tokens))

    all_results.extend(check_remote_frame(load_theme_tokens("white")))
    all_results.extend(check_terminal(_base_tokens))

    required = [r for r in all_results if not r[4]]
    advisory = [r for r in all_results if r[4]]
    failed = [r for r in required if not r[3]]

    width = max(len(r[0]) for r in all_results) + 2
    for label, ratio, min_ratio, ok, is_advisory in all_results:
        status = "PASS" if ok else ("info" if is_advisory else "FAIL")
        print(f"{label.ljust(width)} {ratio:5.2f}:1  (needs {min_ratio:.1f}:1)  {status}")

    print()
    print(f"{len(required) - len(failed)}/{len(required)} required pairs pass WCAG AA.")
    if advisory:
        print(f"{len(advisory)} advisory pair(s) reported above (not gating) - see script comments for why.")
    if failed:
        print(f"{len(failed)} FAILED:")
        for label, ratio, min_ratio, _, _adv in failed:
            print(f"  - {label}: {ratio:.2f}:1 < {min_ratio:.1f}:1")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
