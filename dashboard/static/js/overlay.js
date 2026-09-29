/**
 * OBS-Style Text Overlay Studio Client
 * Handles real-time interactive canvas preview, dragging, snap-to-corner anchors,
 * typography styling engine, and zero-latency sync to Chrome kiosk on Display :99.
 */
(function() {
  'use strict';

  // Read initial state
  let state = {};
  try {
    const raw = document.getElementById('overlay-state-init');
    if (raw) state = JSON.parse(raw.textContent);
  } catch (e) {
    state = {};
  }

  // DOM Elements
  const stage = document.getElementById('canvas-stage');
  const box = document.getElementById('draggable-overlay-box');
  const render = document.getElementById('overlay-content-render');
  const readoutX = document.getElementById('readout-x');
  const readoutY = document.getElementById('readout-y');

  const btnToggle = document.getElementById('btn-toggle-overlay');
  const btnToggleText = document.getElementById('btn-toggle-text');
  const statusBadge = document.getElementById('overlay-status-badge');
  const statusText = document.getElementById('overlay-status-text');
  const btnSave = document.getElementById('btn-save-overlay');

  // Input Controls
  const inputTextInput = document.getElementById('overlay-text-input');
  const inputFontFamily = document.getElementById('overlay-font-family') || document.getElementById('fontFamily');
  const inputFontSize = document.getElementById('overlay-font-size');
  const fontSizeVal = document.getElementById('font-size-val');

  const inputFontColor = document.getElementById('overlay-font-color');
  const inputFontColorHex = document.getElementById('overlay-font-color-hex');
  const inputFontOpacity = document.getElementById('overlay-font-opacity');
  const fontOpacityVal = document.getElementById('font-opacity-val');

  const toggleOutline = document.getElementById('toggle-outline');
  const groupOutlineBody = document.getElementById('group-outline-body');
  const inputOutlineColor = document.getElementById('overlay-outline-color');
  const inputOutlineColorHex = document.getElementById('overlay-outline-color-hex');
  const inputOutlineWidth = document.getElementById('overlay-outline-width');
  const outlineWidthVal = document.getElementById('outline-width-val');

  const toggleBox = document.getElementById('toggle-box');
  const groupBoxBody = document.getElementById('group-box-body');
  const inputBoxColor = document.getElementById('overlay-box-color');
  const inputBoxColorHex = document.getElementById('overlay-box-color-hex');
  const inputBoxOpacity = document.getElementById('overlay-box-opacity');
  const boxOpacityVal = document.getElementById('box-opacity-val');
  const inputBoxPadding = document.getElementById('overlay-box-padding');
  const boxPaddingVal = document.getElementById('box-padding-val');
  const inputBoxRadius = document.getElementById('overlay-box-radius');
  const boxRadiusVal = document.getElementById('box-radius-val');

  const toggleShadow = document.getElementById('toggle-shadow');
  const groupShadowBody = document.getElementById('group-shadow-body');
  const inputShadowColor = document.getElementById('overlay-shadow-color');
  const inputShadowColorHex = document.getElementById('overlay-shadow-color-hex');
  const inputShadowBlur = document.getElementById('overlay-shadow-blur');
  const shadowBlurVal = document.getElementById('shadow-blur-val');
  const inputShadowX = document.getElementById('overlay-shadow-x');
  const shadowXVal = document.getElementById('shadow-x-val');
  const inputShadowY = document.getElementById('overlay-shadow-y');
  const shadowYVal = document.getElementById('shadow-y-val');

  const snapButtons = document.querySelectorAll('.snap-btn');

  function hexToRgba(hex, opacityPct) {
    let c = (hex || '#000000').replace('#', '');
    if (c.length === 3) c = c.split('').map(x => x + x).join('');
    if (c.length !== 6) c = '000000';
    const r = parseInt(c.substring(0, 2), 16) || 0;
    const g = parseInt(c.substring(2, 4), 16) || 0;
    const b = parseInt(c.substring(4, 6), 16) || 0;
    const a = Math.max(0, Math.min(1, (opacityPct !== undefined ? opacityPct : 100) / 100));
    return `rgba(${r}, ${g}, ${b}, ${a.toFixed(2)})`;
  }

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  function loadGoogleFont(fontFamily) {
    if (!fontFamily) return;
    const fontUrlFamily = fontFamily.replace(/\s+/g, '+');
    const fontId = 'font-preview-' + fontFamily.toLowerCase().replace(/[^a-z0-9]/g, '-');
    if (!document.getElementById(fontId)) {
      const link = document.createElement('link');
      link.id = fontId;
      link.rel = 'stylesheet';
      link.href = 'https://fonts.googleapis.com/css2?family=' + fontUrlFamily + '&display=swap';
      document.head.appendChild(link);
    }
  }

  // Update Visual Preview Box
  function updatePreview() {
    if (!stage || !box || !render) return;
    loadGoogleFont(state.font_family);

    // Stage scale factor (relative to 1920x1080 canvas)
    const stageWidth = stage.clientWidth || 960;
    const scale = stageWidth / 1920;

    // Position
    const posX = parseFloat(state.pos_x) || 5;
    const posY = parseFloat(state.pos_y) || 88;
    box.style.left = posX + '%';
    box.style.top = posY + '%';

    // Anchor transform
    const anchor = state.anchor || 'custom';
    if (anchor === 'top-left') {
      box.style.transform = 'translate(0, 0)';
    } else if (anchor === 'top-right') {
      box.style.transform = 'translate(-100%, 0)';
    } else if (anchor === 'bottom-left') {
      box.style.transform = 'translate(0, -100%)';
    } else if (anchor === 'bottom-right') {
      box.style.transform = 'translate(-100%, -100%)';
    } else if (anchor === 'center') {
      box.style.transform = 'translate(-50%, -50%)';
    } else {
      const tx = posX > 70 ? '-100%' : (posX > 30 ? '-50%' : '0');
      const ty = posY > 70 ? '-100%' : (posY > 30 ? '-50%' : '0');
      box.style.transform = `translate(${tx}, ${ty})`;
    }

    // Coords readout
    if (readoutX) readoutX.textContent = posX.toFixed(1) + '%';
    if (readoutY) readoutY.textContent = posY.toFixed(1) + '%';

    // Text & Font
    render.textContent = state.text || 'LIVE BROADCAST';
    render.style.fontFamily = `"${state.font_family || 'Montserrat'}", sans-serif`;
    const scaledFontSize = Math.max(10, (state.font_size || 42) * scale);
    render.style.fontSize = scaledFontSize + 'px';
    render.style.color = hexToRgba(state.font_color || '#FFFFFF', state.font_opacity);

    // Outline / Stroke
    if (state.outline_enabled) {
      const scaledStroke = Math.max(1, (state.outline_width || 2) * scale);
      render.style.webkitTextStroke = `${scaledStroke}px ${state.outline_color || '#000000'}`;
    } else {
      render.style.webkitTextStroke = '0';
    }

    // Background Box
    if (state.box_enabled) {
      box.style.backgroundColor = hexToRgba(state.box_color || '#000000', state.box_opacity);
      const scaledPadding = (state.box_padding || 16) * scale;
      const scaledRadius = (state.box_radius || 8) * scale;
      box.style.padding = `${scaledPadding}px`;
      box.style.borderRadius = `${scaledRadius}px`;
    } else {
      box.style.backgroundColor = 'transparent';
      box.style.padding = '0';
      box.style.borderRadius = '0';
    }

    // Drop Shadow
    if (state.shadow_enabled) {
      const sx = (state.shadow_x || 2) * scale;
      const sy = (state.shadow_y || 4) * scale;
      const blur = (state.shadow_blur || 10) * scale;
      render.style.textShadow = `${sx}px ${sy}px ${blur}px ${state.shadow_color || '#000000'}`;
    } else {
      render.style.textShadow = 'none';
    }

    // Status & Toggle Button
    const isVis = !!state.visible;
    if (statusBadge) {
      statusBadge.setAttribute('data-visible', isVis ? 'true' : 'false');
    }
    if (statusText) {
      statusText.textContent = isVis ? 'ON-AIR (VISIBLE)' : 'OFF-AIR (HIDDEN)';
    }
    if (btnToggle) {
      btnToggle.className = `btn btn-touch ${isVis ? 'btn-danger' : 'btn-primary'}`;
    }
    if (btnToggleText) {
      btnToggleText.textContent = isVis ? 'HIDE OVERLAY' : 'SHOW OVERLAY';
    }
  }

  // Sync state to API (debounced or explicit)
  let saveTimer = null;
  async function saveState(showToast = false) {
    try {
      const res = await fetch('/api/overlay', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': getCsrfToken(),
        },
        body: JSON.stringify(state),
      });
      if (res.ok) {
        const data = await res.json();
        if (data && data.state) state = data.state;
        updatePreview();
        if (typeof syncWatermarkModeUI === 'function') syncWatermarkModeUI();
        if (typeof setRequiresRestart === 'function' && 'requires_restart' in data) setRequiresRestart(data.requires_restart);
        if (typeof checkHardwareStatus === 'function') checkHardwareStatus();
        if (showToast && typeof window.toast === 'function') {
          window.toast('Overlay applied to live broadcast.', 'success');
        }
      }
    } catch (e) {
      console.warn('Overlay save error:', e);
    }
  }

  function queueAutoSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveState(false);
    }, 600);
  }

  // --- Drag and Drop Logic ---
  let isDragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let elementStartPercentX = 0;
  let elementStartPercentY = 0;

  function onPointerDown(e) {
    if (!stage || !box) return;
    isDragging = true;
    box.classList.add('is-dragging');
    state.anchor = 'custom';

    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    dragStartX = clientX;
    dragStartY = clientY;
    elementStartPercentX = parseFloat(state.pos_x) || 5;
    elementStartPercentY = parseFloat(state.pos_y) || 88;

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('touchmove', onPointerMove, { passive: false });
    window.addEventListener('touchend', onPointerUp);
  }

  function onPointerMove(e) {
    if (!isDragging || !stage) return;
    if (e.cancelable) e.preventDefault();

    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;

    const deltaX = clientX - dragStartX;
    const deltaY = clientY - dragStartY;

    const stageRect = stage.getBoundingClientRect();
    const percentDeltaX = (deltaX / stageRect.width) * 100;
    const percentDeltaY = (deltaY / stageRect.height) * 100;

    let newX = elementStartPercentX + percentDeltaX;
    let newY = elementStartPercentY + percentDeltaY;

    // Clamp within 0% - 100%
    newX = Math.max(0, Math.min(100, Math.round(newX * 10) / 10));
    newY = Math.max(0, Math.min(100, Math.round(newY * 10) / 10));

    state.pos_x = newX;
    state.pos_y = newY;

    updatePreview();
  }

  function onPointerUp() {
    if (!isDragging) return;
    isDragging = false;
    if (box) box.classList.remove('is-dragging');

    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('touchmove', onPointerMove);
    window.removeEventListener('touchend', onPointerUp);

    queueAutoSave();
  }

  if (box) {
    box.addEventListener('pointerdown', onPointerDown);
    box.addEventListener('touchstart', onPointerDown, { passive: false });
  }

  // Snap to corner buttons
  snapButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const snap = btn.getAttribute('data-snap');
      state.anchor = snap;
      if (snap === 'top-left') {
        state.pos_x = 5.0;
        state.pos_y = 5.0;
      } else if (snap === 'top-right') {
        state.pos_x = 95.0;
        state.pos_y = 5.0;
      } else if (snap === 'center') {
        state.pos_x = 50.0;
        state.pos_y = 50.0;
      } else if (snap === 'bottom-left') {
        state.pos_x = 5.0;
        state.pos_y = 95.0;
      } else if (snap === 'bottom-right') {
        state.pos_x = 95.0;
        state.pos_y = 95.0;
      }
      updatePreview();
      queueAutoSave();
    });
  });

  // --- Input Change Listeners ---
  if (inputTextInput) {
    inputTextInput.addEventListener('input', () => {
      state.text = inputTextInput.value;
      updatePreview();
      queueAutoSave();
    });
  }

  if (inputFontFamily) {
    inputFontFamily.addEventListener('change', () => {
      state.font_family = inputFontFamily.value;
      updatePreview();
      queueAutoSave();
    });
  }

  if (inputFontSize) {
    inputFontSize.addEventListener('input', () => {
      state.font_size = parseInt(inputFontSize.value, 10);
      if (fontSizeVal) fontSizeVal.textContent = state.font_size + ' px';
      updatePreview();
      queueAutoSave();
    });
  }

  // Font color + hex sync
  if (inputFontColor) {
    inputFontColor.addEventListener('input', () => {
      state.font_color = inputFontColor.value;
      if (inputFontColorHex) inputFontColorHex.value = state.font_color;
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputFontColorHex) {
    inputFontColorHex.addEventListener('input', () => {
      if (/^#[0-9A-Fa-f]{6}$/.test(inputFontColorHex.value)) {
        state.font_color = inputFontColorHex.value;
        if (inputFontColor) inputFontColor.value = state.font_color;
        updatePreview();
        queueAutoSave();
      }
    });
  }

  if (inputFontOpacity) {
    inputFontOpacity.addEventListener('input', () => {
      state.font_opacity = parseInt(inputFontOpacity.value, 10);
      if (fontOpacityVal) fontOpacityVal.textContent = state.font_opacity + '%';
      updatePreview();
      queueAutoSave();
    });
  }

  // Outline / Stroke
  if (toggleOutline) {
    toggleOutline.addEventListener('change', () => {
      state.outline_enabled = toggleOutline.checked;
      if (groupOutlineBody) {
        groupOutlineBody.style.opacity = state.outline_enabled ? '1' : '0.5';
        groupOutlineBody.style.pointerEvents = state.outline_enabled ? 'auto' : 'none';
      }
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputOutlineColor) {
    inputOutlineColor.addEventListener('input', () => {
      state.outline_color = inputOutlineColor.value;
      if (inputOutlineColorHex) inputOutlineColorHex.value = state.outline_color;
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputOutlineColorHex) {
    inputOutlineColorHex.addEventListener('input', () => {
      if (/^#[0-9A-Fa-f]{6}$/.test(inputOutlineColorHex.value)) {
        state.outline_color = inputOutlineColorHex.value;
        if (inputOutlineColor) inputOutlineColor.value = state.outline_color;
        updatePreview();
        queueAutoSave();
      }
    });
  }
  if (inputOutlineWidth) {
    inputOutlineWidth.addEventListener('input', () => {
      state.outline_width = parseInt(inputOutlineWidth.value, 10);
      if (outlineWidthVal) outlineWidthVal.textContent = state.outline_width + ' px';
      updatePreview();
      queueAutoSave();
    });
  }

  // Background Box
  if (toggleBox) {
    toggleBox.addEventListener('change', () => {
      state.box_enabled = toggleBox.checked;
      if (groupBoxBody) {
        groupBoxBody.style.opacity = state.box_enabled ? '1' : '0.5';
        groupBoxBody.style.pointerEvents = state.box_enabled ? 'auto' : 'none';
      }
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputBoxColor) {
    inputBoxColor.addEventListener('input', () => {
      state.box_color = inputBoxColor.value;
      if (inputBoxColorHex) inputBoxColorHex.value = state.box_color;
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputBoxColorHex) {
    inputBoxColorHex.addEventListener('input', () => {
      if (/^#[0-9A-Fa-f]{6}$/.test(inputBoxColorHex.value)) {
        state.box_color = inputBoxColorHex.value;
        if (inputBoxColor) inputBoxColor.value = state.box_color;
        updatePreview();
        queueAutoSave();
      }
    });
  }
  if (inputBoxOpacity) {
    inputBoxOpacity.addEventListener('input', () => {
      state.box_opacity = parseInt(inputBoxOpacity.value, 10);
      if (boxOpacityVal) boxOpacityVal.textContent = state.box_opacity + '%';
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputBoxPadding) {
    inputBoxPadding.addEventListener('input', () => {
      state.box_padding = parseInt(inputBoxPadding.value, 10);
      if (boxPaddingVal) boxPaddingVal.textContent = state.box_padding + ' px';
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputBoxRadius) {
    inputBoxRadius.addEventListener('input', () => {
      state.box_radius = parseInt(inputBoxRadius.value, 10);
      if (boxRadiusVal) boxRadiusVal.textContent = state.box_radius + ' px';
      updatePreview();
      queueAutoSave();
    });
  }

  // Drop Shadow
  if (toggleShadow) {
    toggleShadow.addEventListener('change', () => {
      state.shadow_enabled = toggleShadow.checked;
      if (groupShadowBody) {
        groupShadowBody.style.opacity = state.shadow_enabled ? '1' : '0.5';
        groupShadowBody.style.pointerEvents = state.shadow_enabled ? 'auto' : 'none';
      }
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputShadowColor) {
    inputShadowColor.addEventListener('input', () => {
      state.shadow_color = inputShadowColor.value;
      if (inputShadowColorHex) inputShadowColorHex.value = state.shadow_color;
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputShadowColorHex) {
    inputShadowColorHex.addEventListener('input', () => {
      if (/^#[0-9A-Fa-f]{6}$/.test(inputShadowColorHex.value)) {
        state.shadow_color = inputShadowColorHex.value;
        if (inputShadowColor) inputShadowColor.value = state.shadow_color;
        updatePreview();
        queueAutoSave();
      }
    });
  }
  if (inputShadowBlur) {
    inputShadowBlur.addEventListener('input', () => {
      state.shadow_blur = parseInt(inputShadowBlur.value, 10);
      if (shadowBlurVal) shadowBlurVal.textContent = state.shadow_blur + ' px';
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputShadowX) {
    inputShadowX.addEventListener('input', () => {
      state.shadow_x = parseInt(inputShadowX.value, 10);
      if (shadowXVal) shadowXVal.textContent = state.shadow_x + ' px';
      updatePreview();
      queueAutoSave();
    });
  }
  if (inputShadowY) {
    inputShadowY.addEventListener('input', () => {
      state.shadow_y = parseInt(inputShadowY.value, 10);
      if (shadowYVal) shadowYVal.textContent = state.shadow_y + ' px';
      updatePreview();
      queueAutoSave();
    });
  }

  // Master Toggle Button
  if (btnToggle) {
    btnToggle.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/overlay/toggle', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': getCsrfToken(),
          },
        });
        if (res.ok) {
          const data = await res.json();
          if (data && data.state) state = data.state;
          updatePreview();
          syncWatermarkModeUI();
          if ('requires_restart' in data) setRequiresRestart(data.requires_restart);
          checkHardwareStatus();
          if (typeof window.toast === 'function') {
            window.toast(state.visible ? 'Overlay is now ON-AIR' : 'Overlay is now HIDDEN', state.visible ? 'success' : 'info');
          }
        }
      } catch (e) {
        console.error('Toggle overlay failed:', e);
      }
    });
  }

  // Explicit Save Button
  if (btnSave) {
    btnSave.addEventListener('click', () => {
      saveState(true);
    });
  }

  // Hardware Status Indicator & Force Re-Inject
  const hwBadge = document.getElementById('overlay-hw-badge');
  const hwText = document.getElementById('overlay-hw-text');
  const btnReinject = document.getElementById('btn-reinject-overlay');
  const btnReinjectText = document.getElementById('btn-reinject-text');

  async function checkHardwareStatus() {
    updateWatermarkEncoderBadge();
    if (!hwBadge || !hwText) return;
    try {
      const res = await fetch('/api/overlay/status');
      if (!res.ok) return;
      const data = await res.json();
      hwBadge.classList.remove('status-active', 'status-waiting', 'status-hidden');
      if (!data.connected) {
        hwBadge.classList.add('status-waiting');
        hwText.textContent = '🟡 WAITING FOR STREAM / CDP';
      } else if (data.connected && data.injected && data.visible) {
        hwBadge.classList.add('status-active');
        hwText.textContent = '🟢 OVERLAY ACTIVE ON DISPLAY :99';
      } else if (data.connected && data.injected && !data.visible) {
        hwBadge.classList.add('status-hidden');
        hwText.textContent = '⚪ OVERLAY HIDDEN';
      } else {
        hwBadge.classList.add('status-waiting');
        hwText.textContent = '🟡 READY TO INJECT';
      }
    } catch (e) {
      if (hwBadge && hwText) {
        hwBadge.classList.remove('status-active', 'status-waiting', 'status-hidden');
        hwBadge.classList.add('status-waiting');
        hwText.textContent = '🟡 WAITING FOR STREAM / CDP';
      }
    }
  }

  // --- Part 4: real (encoder-burned) watermark controls ---
  const wmModeButtons = document.querySelectorAll('.wm-mode-btn');
  const wmAnchorButtons = document.querySelectorAll('.wm-anchor-btn');
  const wmFontField = document.getElementById('watermark-font-field');
  const wmFontSelect = document.getElementById('watermark-font-select');
  const wmImageField = document.getElementById('watermark-image-field');
  const wmImageUpload = document.getElementById('watermark-image-upload');
  const wmImageCurrent = document.getElementById('watermark-image-current');
  const wmMarginX = document.getElementById('watermark-margin-x');
  const wmMarginY = document.getElementById('watermark-margin-y');
  const wmSize = document.getElementById('watermark-size');
  const wmSizeUnit = document.getElementById('watermark-size-unit');
  const wmOpacity = document.getElementById('watermark-opacity');
  const wmRestartBanner = document.getElementById('watermark-restart-banner');
  const wmRestartBtn = document.getElementById('btn-watermark-restart-encoder');
  const wmEncoderBadge = document.getElementById('watermark-encoder-badge');
  const wmEncoderText = document.getElementById('watermark-encoder-text');

  function setRequiresRestart(requiresRestart) {
    if (wmRestartBanner) wmRestartBanner.hidden = !requiresRestart;
  }

  function syncWatermarkModeUI() {
    const mode = state.mode || 'text';
    wmModeButtons.forEach((b) => b.classList.toggle('btn-primary', b.dataset.mode === mode));
    wmModeButtons.forEach((b) => b.classList.toggle('btn-secondary', b.dataset.mode !== mode));
    if (wmFontField) wmFontField.hidden = mode !== 'text';
    if (wmImageField) wmImageField.hidden = mode !== 'image';
    if (wmSizeUnit) wmSizeUnit.textContent = mode === 'image' ? '(% of video width)' : '(px)';
    if (wmSize) wmSize.value = mode === 'image' ? (state.image_scale_pct != null ? state.image_scale_pct : 15) : (state.font_size || 28);
    if (wmOpacity) wmOpacity.value = mode === 'image' ? (state.image_opacity != null ? state.image_opacity : 100) : (state.font_opacity != null ? state.font_opacity : 100);
    wmAnchorButtons.forEach((b) => b.classList.toggle('is-active', b.dataset.anchor === state.anchor));
    if (wmImageCurrent) wmImageCurrent.textContent = state.image_path ? ('Current: ' + state.image_path.split('/').pop()) : 'No image uploaded yet.';
  }

  async function updateWatermarkEncoderBadge() {
    if (!wmEncoderBadge || !wmEncoderText) return;
    try {
      const res = await fetch('/api/overlay/status');
      if (!res.ok) return;
      const data = await res.json();
      wmEncoderBadge.classList.remove('status-active', 'status-hidden');
      if (data.encoder_active) {
        wmEncoderBadge.classList.add('status-active');
        wmEncoderText.textContent = 'LIVE: WATERMARK ON';
      } else {
        wmEncoderBadge.classList.add('status-hidden');
        wmEncoderText.textContent = 'LIVE: WATERMARK OFF';
      }
      setRequiresRestart(Boolean(data.requires_restart));
    } catch (e) { /* leave last-known state on screen */ }
  }

  wmModeButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      state.mode = btn.dataset.mode;
      syncWatermarkModeUI();
      queueAutoSave();
    });
  });

  wmAnchorButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      state.anchor = btn.dataset.anchor;
      syncWatermarkModeUI();
      queueAutoSave();
    });
  });

  if (wmFontSelect) {
    wmFontSelect.value = state.encoder_font || 'inter';
    wmFontSelect.addEventListener('change', () => {
      state.encoder_font = wmFontSelect.value;
      queueAutoSave();
    });
  }

  if (wmMarginX) wmMarginX.addEventListener('input', () => { state.margin_x = parseInt(wmMarginX.value, 10) || 0; queueAutoSave(); });
  if (wmMarginY) wmMarginY.addEventListener('input', () => { state.margin_y = parseInt(wmMarginY.value, 10) || 0; queueAutoSave(); });
  if (wmSize) wmSize.addEventListener('input', () => {
    if ((state.mode || 'text') === 'image') state.image_scale_pct = parseFloat(wmSize.value);
    else state.font_size = parseInt(wmSize.value, 10);
    queueAutoSave();
  });
  if (wmOpacity) wmOpacity.addEventListener('input', () => {
    if ((state.mode || 'text') === 'image') state.image_opacity = parseInt(wmOpacity.value, 10);
    else state.font_opacity = parseInt(wmOpacity.value, 10);
    queueAutoSave();
  });

  if (wmImageUpload) {
    wmImageUpload.addEventListener('change', async () => {
      const file = wmImageUpload.files && wmImageUpload.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append('image', file);
      try {
        const res = await fetch('/api/overlay/image', {
          method: 'POST',
          headers: { 'X-CSRF-Token': getCsrfToken() },
          body: formData,
        });
        const data = await res.json();
        if (!res.ok) {
          if (typeof window.toast === 'function') window.toast(data.detail || 'Upload failed', 'err');
          return;
        }
        if (data.state) state = data.state;
        syncWatermarkModeUI();
        if (typeof window.toast === 'function') window.toast('Watermark image uploaded. Restart the encoder to apply.', 'success');
        checkHardwareStatus();
      } catch (e) {
        if (typeof window.toast === 'function') window.toast('Upload failed', 'err');
      }
    });
  }

  if (wmRestartBtn) {
    wmRestartBtn.addEventListener('click', async () => {
      if (typeof window.confirmDialog === 'function') {
        const ok = await window.confirmDialog('Restart the encoder to apply the watermark change? This briefly interrupts the live stream.');
        if (!ok) return;
      }
      try {
        await fetch('/api/stream/restart', { method: 'POST', headers: { 'X-CSRF-Token': getCsrfToken() } });
        if (typeof window.toast === 'function') window.toast('Encoder restart sent', 'success');
        setTimeout(checkHardwareStatus, 3000);
      } catch (e) {
        if (typeof window.toast === 'function') window.toast('Restart failed', 'err');
      }
    });
  }

  syncWatermarkModeUI();

  if (btnReinject) {
    btnReinject.addEventListener('click', async () => {
      const originalText = btnReinjectText ? btnReinjectText.textContent : 'Force Re-Inject';
      if (btnReinjectText) btnReinjectText.textContent = 'Injecting...';
      btnReinject.disabled = true;
      try {
        const res = await fetch('/api/overlay/reinject', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': getCsrfToken(),
          },
        });
        if (res.ok) {
          const data = await res.json();
          await checkHardwareStatus();
          if (typeof window.toast === 'function') {
            const injected = data.status && data.status.injected;
            window.toast(
              injected ? 'Overlay successfully re-injected on Display :99' : 'Re-injected (kiosk status updated)',
              injected ? 'success' : 'info'
            );
          }
        } else {
          if (typeof window.toast === 'function') {
            window.toast('Failed to re-inject overlay', 'danger');
          }
        }
      } catch (e) {
        console.error('Reinject failed:', e);
        if (typeof window.toast === 'function') {
          window.toast('Network error re-injecting overlay', 'danger');
        }
      } finally {
        if (btnReinjectText) btnReinjectText.textContent = originalText;
        btnReinject.disabled = false;
      }
    });
  }

  // --- Custom Visual Font Picker ---
  function initCustomFontPicker() {
    const picker = document.getElementById('custom-font-picker');
    if (!picker) return;

    const trigger = document.getElementById('font-picker-trigger');
    const dropdown = document.getElementById('font-picker-dropdown');
    const searchInput = document.getElementById('font-picker-search');
    const searchClear = document.getElementById('font-picker-search-clear');
    const triggerName = document.getElementById('font-trigger-name');
    const triggerBadge = document.getElementById('font-trigger-badge');
    const list = document.getElementById('font-picker-list');
    const emptyMsg = document.getElementById('font-picker-empty');
    const items = Array.from(picker.querySelectorAll('.font-picker-item'));
    const groups = Array.from(picker.querySelectorAll('.font-picker-category-group'));

    function openPicker() {
      picker.classList.add('is-open');
      if (trigger) trigger.setAttribute('aria-expanded', 'true');
      if (dropdown) dropdown.removeAttribute('hidden');
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
      const active = picker.querySelector('.font-picker-item.is-selected');
      if (active) {
        active.scrollIntoView({ block: 'nearest' });
      }
    }

    function closePicker() {
      picker.classList.remove('is-open');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      if (dropdown) dropdown.setAttribute('hidden', '');
    }

    function togglePicker() {
      if (picker.classList.contains('is-open')) {
        closePicker();
      } else {
        openPicker();
      }
    }

    if (trigger) {
      trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        togglePicker();
      });
    }

    // Close on outside click
    document.addEventListener('click', (e) => {
      if (!picker.contains(e.target)) {
        closePicker();
      }
    });

    // Close on Escape
    picker.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closePicker();
        if (trigger) trigger.focus();
      }
    });

    // Selection handler
    function selectFont(fontName, isSinhala) {
      if (inputFontFamily) {
        inputFontFamily.value = fontName;
        inputFontFamily.dispatchEvent(new Event('change', { bubbles: true }));
      }
      if (triggerName) {
        triggerName.textContent = fontName;
        triggerName.style.fontFamily = `"${fontName}", sans-serif`;
      }
      if (triggerBadge) {
        triggerBadge.textContent = isSinhala ? 'අආ ශ්‍රී' : 'Ag 123';
      }

      items.forEach((item) => {
        const isMatch = item.getAttribute('data-font') === fontName;
        item.classList.toggle('is-selected', isMatch);
        item.setAttribute('aria-selected', isMatch ? 'true' : 'false');
      });

      closePicker();
      if (trigger) trigger.focus();
    }

    items.forEach((item) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const fontName = item.getAttribute('data-font');
        const isSinhala = item.getAttribute('data-sinhala') === 'true';
        selectFont(fontName, isSinhala);
      });

      item.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          const fontName = item.getAttribute('data-font');
          const isSinhala = item.getAttribute('data-sinhala') === 'true';
          selectFont(fontName, isSinhala);
        }
      });
    });

    // Search / Filter
    function filterFonts() {
      const q = (searchInput.value || '').trim().toLowerCase();
      if (searchClear) {
        searchClear.hidden = !q;
      }
      let visibleCount = 0;

      groups.forEach((grp) => {
        const catName = (grp.getAttribute('data-category') || '').toLowerCase();
        let groupVisible = 0;
        const grpItems = grp.querySelectorAll('.font-picker-item');
        grpItems.forEach((item) => {
          const fontName = (item.getAttribute('data-font') || '').toLowerCase();
          const match = !q || fontName.includes(q) || catName.includes(q);
          item.style.display = match ? 'flex' : 'none';
          if (match) {
            groupVisible++;
            visibleCount++;
          }
        });
        grp.style.display = groupVisible > 0 ? '' : 'none';
      });

      if (emptyMsg) {
        emptyMsg.hidden = visibleCount > 0;
      }
    }

    if (searchInput) {
      searchInput.addEventListener('input', filterFonts);
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          const firstItem = picker.querySelector('.font-picker-item:not([style*="display: none"])');
          if (firstItem) firstItem.focus();
        }
      });
    }

    if (searchClear) {
      searchClear.addEventListener('click', (e) => {
        e.stopPropagation();
        searchInput.value = '';
        filterFonts();
        searchInput.focus();
      });
    }

    // Keyboard navigation within list items
    if (list) {
      list.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          const visibleItems = items.filter((el) => el.style.display !== 'none');
          const currentIdx = visibleItems.indexOf(document.activeElement);
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            const next = visibleItems[currentIdx + 1] || visibleItems[0];
            if (next) next.focus();
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (currentIdx <= 0) {
              if (searchInput) searchInput.focus();
            } else {
              const prev = visibleItems[currentIdx - 1];
              if (prev) prev.focus();
            }
          }
        }
      });
    }
  }

  // Resize listener to re-scale font and padding proportionally
  window.addEventListener('resize', () => {
    updatePreview();
  });

  // Initial render, visual font picker & status check
  initCustomFontPicker();
  updatePreview();
  checkHardwareStatus();
  setInterval(checkHardwareStatus, 4000);
})();
