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
  const inputFontFamily = document.getElementById('overlay-font-family');
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

  // Update Visual Preview Box
  function updatePreview() {
    if (!stage || !box || !render) return;

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

  // Resize listener to re-scale font and padding proportionally
  window.addEventListener('resize', () => {
    updatePreview();
  });

  // Initial render
  updatePreview();
})();
