// Unit tests for static/js/vnc-fit.js (Remote GUI view geometry).
// Run: node --test tests/js/   (tests/test_vnc_fit.py runs this under pytest when node is installed)
import { test } from "node:test";
import assert from "node:assert/strict";
import { computeLayout, describeLayout, measureBars, cropPercent, normalizeMode, DEFAULT_MODE } from "../../static/js/vnc-fit.js";

const FB = { fbW: 1920, fbH: 1080 };
const BAR = 47;

test("default mode is Smart Fit; unknown modes fall back to it", () => {
  assert.equal(DEFAULT_MODE, "smart");
  assert.equal(normalizeMode(null), "smart");
  assert.equal(normalizeMode("zoom-in"), "smart");
  assert.equal(normalizeMode("fill"), "fill");
});

for (const [slotW, slotH] of [[1382, 790], [1064, 470], [1600, 420], [900, 900], [382, 260]]) {
  test(`smart ${slotW}x${slotH}: panel hugs the picture - no bars, nothing cropped, aspect kept`, () => {
    const L = computeLayout({ mode: "smart", slotW, slotH, barH: BAR, ...FB });
    assert.deepEqual(L.bars, { x: 0, y: 0 });
    assert.deepEqual(L.crop, { x: 0, y: 0 });
    assert.ok(L.stage.w <= slotW && L.stage.h + BAR <= slotH, "fits the slot");
    assert.equal(L.device.w, L.stage.w);
    assert.equal(L.device.h, L.stage.h + BAR);
    assert.ok(Math.abs(L.stage.w / L.stage.h - 16 / 9) < 0.01, "16:9 kept");
    // it is the LARGEST such picture: one side touches the slot
    assert.ok(L.stage.w === slotW || L.stage.h + BAR === slotH || slotW - L.stage.w <= 1 || slotH - BAR - L.stage.h <= 1);
  });
}

test("smart fullscreen: bar floats, the screen is the panel, 16:9 screen has no bars", () => {
  const L = computeLayout({ mode: "smart", slotW: 1920, slotH: 1080, barH: BAR, ...FB, fullscreen: true });
  assert.equal(L.barDocked, false);
  assert.deepEqual(L.frame, { x: 0, y: 0, w: 1920, h: 1080 });
  assert.deepEqual(L.bars, { x: 0, y: 0 });
});

test("smart fullscreen on a 16:10 screen: letterbox top/bottom is reported, never cropped", () => {
  const L = computeLayout({ mode: "smart", slotW: 1920, slotH: 1200, barH: BAR, ...FB, fullscreen: true });
  assert.deepEqual(L.crop, { x: 0, y: 0 });
  assert.equal(L.bars.x, 0);
  assert.equal(L.bars.y, 60);
});

test("fit: full panel, picture centred inside it, bars reported", () => {
  const L = computeLayout({ mode: "fit", slotW: 1600, slotH: 800, barH: BAR, ...FB });
  assert.equal(L.barDocked, true);
  assert.deepEqual(L.device, { w: 1600, h: 800 });
  assert.equal(L.stage.h, 800 - BAR);
  assert.equal(L.frame.h, 753);
  assert.equal(L.frame.w, Math.round(1920 * (753 / 1080)));
  assert.equal(L.frame.x, Math.floor((1600 - L.frame.w) / 2));
  assert.equal(L.bars.x, L.frame.x);
  assert.deepEqual(L.crop, { x: 0, y: 0 });
});

test("fill: covers the stage, centred crop measured in framebuffer pixels", () => {
  const L = computeLayout({ mode: "fill", slotW: 1600, slotH: 647, barH: BAR, ...FB });  // stage 1600x600
  assert.ok(L.frame.w >= 1600 && L.frame.h >= 600);
  assert.equal(L.frame.w, 1600);
  assert.equal(L.crop.x, 0);
  assert.equal(L.crop.y, Math.round((900 - 600) / 2 / (1600 / 1920)));  // 180 fb px top and bottom
  assert.ok(cropPercent(L, 1920, 1080) > 30);
  assert.match(describeLayout(L, 1920, 1080), /cropping 180px top & bottom/);
});

test("fill with matching shapes crops nothing", () => {
  const L = computeLayout({ mode: "fill", slotW: 1280, slotH: 720 + BAR, barH: BAR, ...FB });
  assert.deepEqual(L.crop, { x: 0, y: 0 });
  assert.match(describeLayout(L, 1920, 1080), /nothing cropped/);
});

test("native (1:1): frame = stage, scale 1", () => {
  const L = computeLayout({ mode: "native", slotW: 1000, slotH: 600, barH: BAR, ...FB });
  assert.equal(L.scale, 1);
  assert.deepEqual(L.frame, { x: 0, y: 0, w: 1000, h: 600 - BAR });
});

test("no framebuffer yet: a usable box, no NaN", () => {
  const L = computeLayout({ mode: "smart", slotW: 800, slotH: 500, barH: BAR, fbW: 0, fbH: 0 });
  assert.ok(Number.isFinite(L.frame.w) && Number.isFinite(L.frame.h));
  assert.match(describeLayout(L, 0, 0), /waiting/);
});

test("describeLayout reports MEASURED bars, not the plan", () => {
  const L = computeLayout({ mode: "smart", slotW: 1382, slotH: 790, barH: BAR, ...FB });
  assert.match(describeLayout(L, 1920, 1080, { bars: { x: 0, y: 0 } }), /no borders/);
  assert.match(describeLayout(L, 1920, 1080, { bars: { x: 12, y: 0 } }), /12px left & right letterbox/);
});

test("measureBars reads the gaps between stage and canvas rects", () => {
  const m = measureBars({ x: 0, y: 0, width: 1000, height: 600 }, { x: 34, y: 0, width: 932, height: 600 });
  assert.deepEqual(m.bars, { x: 34, y: 0 });
  assert.equal(measureBars(null, {}), null);
});
