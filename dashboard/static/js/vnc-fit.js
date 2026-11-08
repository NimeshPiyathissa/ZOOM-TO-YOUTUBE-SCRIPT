// Remote GUI view geometry - pure functions, no DOM, so they are unit-tested
// with node (tests/js/vnc-fit.test.mjs). vnc.js measures the page, calls
// computeLayout() and applies the boxes it returns.
//
// Where black space came from (measured 2026-11-08 against a 1920x1080
// framebuffer, which is what Xvfb :99 really is - the Zoom window covers
// all of it at +0+0, so the framebuffer itself has no border):
//   - the card had no definite height, so noVNC scaled the canvas to the
//     card's WIDTH and the canvas height then decided the card height: the
//     page grew past the window (1366x657: 250 px below the fold, Zoom's
//     toolbar and the quick bar off-screen);
//   - in fullscreen the quick bar took ~47 px of height from a 16:9 stage,
//     so the 16:9 framebuffer was pillarboxed (160 px each side at 1920x947);
//   - "Fit Window" toggled fit OFF (to Actual 1:1) - its label named the
//     current state, not the action.
// Black inside Zoom's own window (its dark meeting background around
// video tiles) is real framebuffer content and is never cropped here.

export const MODES = ["smart", "fit", "fill", "native"];
export const DEFAULT_MODE = "smart";
export const MODE_LABELS = { smart: "Smart Fit", fit: "Fit", fill: "Fill", native: "1:1" };

export function normalizeMode(mode) {
  return MODES.includes(mode) ? mode : DEFAULT_MODE;
}

const clampInt = (n) => Math.max(0, Math.floor(n));

/**
 * computeLayout({ mode, slotW, slotH, barH, fbW, fbH, fullscreen })
 *   slot*: the area the Remote GUI may use (the card's box).
 *   barH:  the quick bar's height when it is docked under the screen.
 *   fb*:   the remote framebuffer size.
 *   fullscreen: in Smart Fit the quick bar then floats over the top edge
 *               (auto-hides) instead of taking height from the picture.
 * Returns the device box (the visible black panel), the stage (picture
 * area inside it), the frame noVNC renders into (may be bigger than the
 * stage in Fill), the scale, the black bars left in the stage per side,
 * and how many FRAMEBUFFER pixels are cropped per side (Fill only).
 */
export function computeLayout({ mode, slotW, slotH, barH = 0, fbW, fbH, fullscreen = false }) {
  mode = normalizeMode(mode);
  slotW = clampInt(slotW);
  slotH = clampInt(slotH);
  barH = clampInt(barH);
  const haveFb = fbW > 0 && fbH > 0;
  const barDocked = !(mode === "smart" && fullscreen);
  const availW = slotW;
  const availH = Math.max(0, slotH - (barDocked ? barH : 0));
  const out = {
    mode, barDocked,
    device: { w: slotW, h: slotH },
    stage: { w: availW, h: availH },
    frame: { x: 0, y: 0, w: availW, h: availH },
    scale: 1,
    bars: { x: 0, y: 0 },
    crop: { x: 0, y: 0 },
  };
  if (!haveFb || availW === 0 || availH === 0) return out;

  if (mode === "native") {
    // 1:1 pixels; noVNC clips to the stage and lets the user drag to pan.
    out.scale = 1;
    return out;
  }

  const contain = Math.min(availW / fbW, availH / fbH);
  if (mode === "smart") {
    // Shrink the panel itself to the picture, so the leftover space is page
    // background rather than black bars inside the panel.
    const w = Math.min(availW, Math.round(fbW * contain));
    const h = Math.min(availH, Math.round(fbH * contain));
    out.scale = contain;
    out.stage = { w, h };
    out.frame = { x: 0, y: 0, w, h };
    out.device = { w, h: h + (barDocked ? barH : 0) };
    // Only fullscreen can leave bars here (the screen is the panel there).
    if (fullscreen) {
      out.device = { w: slotW, h: slotH };
      out.stage = { w: slotW, h: slotH };
      out.frame = { x: Math.floor((slotW - w) / 2), y: Math.floor((slotH - h) / 2), w, h };
      out.bars = { x: Math.floor((slotW - w) / 2), y: Math.floor((slotH - h) / 2) };
    }
    return out;
  }

  if (mode === "fit") {
    const w = Math.min(availW, Math.round(fbW * contain));
    const h = Math.min(availH, Math.round(fbH * contain));
    out.scale = contain;
    out.frame = { x: Math.floor((availW - w) / 2), y: Math.floor((availH - h) / 2), w, h };
    out.bars = { x: Math.floor((availW - w) / 2), y: Math.floor((availH - h) / 2) };
    return out;
  }

  // fill: cover the stage, centred; the overflow is cropped by the stage.
  const cover = Math.max(availW / fbW, availH / fbH);
  const w = Math.round(fbW * cover);
  const h = Math.round(fbH * cover);
  out.scale = cover;
  out.frame = { x: Math.floor((availW - w) / 2), y: Math.floor((availH - h) / 2), w, h };
  out.crop = {
    x: Math.max(0, Math.round((w - availW) / 2 / cover)),
    y: Math.max(0, Math.round((h - availH) / 2 / cover)),
  };
  return out;
}

/** Share of the framebuffer area hidden by Fill's crop, 0-100. */
export function cropPercent(layout, fbW, fbH) {
  if (!(fbW > 0 && fbH > 0)) return 0;
  const visW = Math.max(0, fbW - 2 * layout.crop.x);
  const visH = Math.max(0, fbH - 2 * layout.crop.y);
  return Math.round(1000 * (1 - (visW * visH) / (fbW * fbH))) / 10;
}

/**
 * One-line, human status for the quick bar / toast. `measured` is what the
 * page actually rendered ({bars:{x,y}} read back from the canvas rect) - the
 * message reports that, not the plan, so it never claims "no borders" that
 * the DOM doesn't show.
 */
export function describeLayout(layout, fbW, fbH, measured) {
  const label = MODE_LABELS[layout.mode];
  if (!(fbW > 0 && fbH > 0)) return `${label} · waiting for the remote screen`;
  const pct = Math.round(layout.scale * 100);
  const size = `${fbW}×${fbH} at ${pct}%`;
  if (layout.mode === "native") return `${label} · ${fbW}×${fbH} · drag to pan`;
  if (layout.mode === "fill") {
    const c = layout.crop;
    if (!c.x && !c.y) return `${label} · ${size} · nothing cropped`;
    const where = c.y ? `${c.y}px top & bottom` : `${c.x}px left & right`;
    return `${label} · cropping ${where} of the remote screen (${cropPercent(layout, fbW, fbH)}%) - Zoom controls at the edges may be hidden`;
  }
  const b = (measured && measured.bars) || layout.bars;
  if (b.x <= 1 && b.y <= 1) return `${label} · ${size} · no borders`;
  const where = b.x > 1 ? `${b.x}px left & right` : `${b.y}px top & bottom`;
  return `${label} · ${size} · ${where} letterbox (window shape differs from the remote screen)`;
}

/** Black bars actually visible, from two DOMRect-like boxes (stage, canvas). */
export function measureBars(stageRect, canvasRect) {
  if (!stageRect || !canvasRect) return null;
  const left = Math.max(0, Math.round(canvasRect.x - stageRect.x));
  const right = Math.max(0, Math.round(stageRect.x + stageRect.width - canvasRect.x - canvasRect.width));
  const top = Math.max(0, Math.round(canvasRect.y - stageRect.y));
  const bottom = Math.max(0, Math.round(stageRect.y + stageRect.height - canvasRect.y - canvasRect.height));
  return { left, right, top, bottom, bars: { x: Math.max(left, right), y: Math.max(top, bottom) } };
}
