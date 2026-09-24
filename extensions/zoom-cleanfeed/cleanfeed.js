/**
 * Zoom Web Kiosk Clean-Feed Content Script
 * - Enforces right-click context menu suppression
 * - Injects and locks cleanfeed stylesheet
 * - Continuously purges header/footer bars, toolbars, participant name tags
 * - Forces full-canvas video fitting (100vw x 100vh, object-fit: contain, position: fixed)
 * - Persistent MutationObserver to defeat dynamic Zoom re-mounting
 */
(function() {
  'use strict';

  const CSS_TEXT = `
    /* 1. Eliminate Top Header & All Sub-Headers */
    .meeting-app-header, #header, .header, .topic, .meeting-info-header,
    .meeting-info-icon__header, .meeting-topic,
    [class*="header"], [class*="meeting-app-header"], [class*="header__"], [class*="meeting-header"],
    .suspension-header, div[role="banner"], .meeting-client-head,
    #header_container, .header-container, [id="header_container"] {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        min-height: 0 !important;
        max-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
    }

    /* 2. Eliminate Bottom Control Bar & Floating Tools */
    .footer, #wc-footer, .meeting-control-bar, .footer__control-bar,
    [class*="footer"], div[role="toolbar"], #foot-bar,
    .footer-bar, .room-footer, .more-button, .audio-option-menu,
    .settings-dialog, .suspension-window, .security-option-menu,
    .footer-button-base, .leave-btn-container, [class*="leave-btn"],
    .meeting-client-inner .footer, [class*="meeting-control-bar"],
    [class*="footer__control-bar"], [class*="footer-button"],
    #footer_container, .footer-container, [id="footer_container"],
    #livesdk__campaign, [class*="livesdk"], [id*="livesdk"],
    .livesdk__placement, .livesdk__invitation, .livesdk__Draggable {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        min-height: 0 !important;
        max-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
    }

    /* 3. Strip Participant Tags & Badges */
    .participant-name, .video-box__name-tag, [class*="speaker-bar"],
    [class*="name-tag"], #speaker-box-name, .video-avatar__avatar-name,
    .speaker-bar, .name-label, [class*="speaker-name"],
    [class*="participant-name"], .speaker-active-name,
    .can-hide.participant-name, .aria-label-participant-name {
        display: none !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }

    /* 4. Eliminate Black Bars & Force 100vw x 100vh Edge-to-Edge */
    html, body, #root, #app, .main-layout, .meeting-client, .meeting-client-inner, .window-content,
    .video-container, .gallery-video-container, .speaker-view, .single-view,
    .full-screen-video, .video-player-container, #video-container, .video-box,
    #content_container, .zoom-newcontent, .total-main-content, #content, .main-content,
    .react-draggable, [class*="main-layout"], [class*="meeting-client"], [class*="video-container"] {
        width: 100vw !important;
        height: 100vh !important;
        max-width: 100vw !important;
        max-height: 100vh !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
        background-color: #000 !important;
        background: #000 !important;
        box-sizing: border-box !important;
    }

    /* Force active video container and canvas to fill the entire screen */
    .speaker-active-video, .video-avatar-container, .speaker-view,
    video, canvas, canvas.speaker-active-video__canvas, div[class*="active-video"] {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        max-width: 100vw !important;
        max-height: 100vh !important;
        object-fit: contain !important;
        margin: 0 !important;
        padding: 0 !important;
        z-index: 1 !important;
    }

    ::-webkit-scrollbar {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
    }
  `;

  // 1. Right-Click Context Lock
  const suppressEvent = function(e) {
    if (e) {
      if (typeof e.preventDefault === 'function') e.preventDefault();
      if (typeof e.stopPropagation === 'function') e.stopPropagation();
      if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
    }
    return false;
  };

  if (!window.__cleanfeed_events_bound) {
    window.addEventListener('contextmenu', suppressEvent, true);
    document.addEventListener('contextmenu', suppressEvent, true);
    window.addEventListener('auxclick', function(e) {
      if (e && e.button === 2) suppressEvent(e);
    }, true);
    window.addEventListener('keydown', function(e) {
      if (e && (e.key === 'ContextMenu' || e.keyCode === 93)) suppressEvent(e);
    }, true);
    window.__cleanfeed_events_bound = true;
  }

  // 2. Ensure Style Element Exists
  function ensureStyle() {
    let styleEl = document.getElementById('zoom-cleanfeed-style');
    if (!styleEl) {
      styleEl = document.createElement('style');
      styleEl.id = 'zoom-cleanfeed-style';
      styleEl.textContent = CSS_TEXT;
      (document.head || document.documentElement).appendChild(styleEl);
    }
  }

  // 3. Selective Programmatic DOM Sanitizer
  const HIDE_SELECTORS = [
    '.meeting-app-header', '#header', '.header', '.topic', '.meeting-info-header',
    '.meeting-info-icon__header', '.meeting-topic',
    '[class*="header"]', '[class*="meeting-app-header"]', '[class*="header__"]', '[class*="meeting-header"]',
    '.suspension-header', 'div[role="banner"]', '.meeting-client-head',
    '#header_container', '.header-container', '[id="header_container"]',
    '.footer', '#wc-footer', '.meeting-control-bar', '.footer__control-bar',
    '[class*="footer"]', 'div[role="toolbar"]', '#foot-bar',
    '.footer-bar', '.room-footer', '.more-button', '.audio-option-menu',
    '.settings-dialog', '.suspension-window', '.security-option-menu',
    '.footer-button-base', '.leave-btn-container', '[class*="leave-btn"]',
    '.meeting-client-inner .footer', '[class*="meeting-control-bar"]',
    '[class*="footer__control-bar"]', '[class*="footer-button"]',
    '#footer_container', '.footer-container', '[id="footer_container"]',
    '#livesdk__campaign', '[class*="livesdk"]', '[id*="livesdk"]',
    '.livesdk__placement', '.livesdk__invitation', '.livesdk__Draggable',
    '.participant-name', '.video-box__name-tag', '[class*="speaker-bar"]',
    '[class*="name-tag"]', '#speaker-box-name', '.video-avatar__avatar-name',
    '.speaker-bar', '.name-label', '[class*="speaker-name"]',
    '[class*="participant-name"]', '.speaker-active-name',
    '.can-hide.participant-name', '.aria-label-participant-name'
  ];

  function purgeUI() {
    ensureStyle();
    try {
      const elements = document.querySelectorAll(HIDE_SELECTORS.join(','));
      for (let i = 0; i < elements.length; i++) {
        const el = elements[i];
        if (el && el.style) {
          el.style.setProperty('display', 'none', 'important');
          el.style.setProperty('opacity', '0', 'important');
          el.style.setProperty('pointer-events', 'none', 'important');
          el.style.setProperty('visibility', 'hidden', 'important');
          el.style.setProperty('height', '0', 'important');
          el.style.setProperty('width', '0', 'important');
        }
      }

      // Enforce 100vw x 100vh edge-to-edge full canvas fitting
      const videos = document.querySelectorAll('.speaker-active-video, .video-avatar-container, .speaker-view, video, canvas, canvas.speaker-active-video__canvas, div[class*="active-video"]');
      for (let i = 0; i < videos.length; i++) {
        const v = videos[i];
        if (v && v.style) {
          v.style.setProperty('position', 'fixed', 'important');
          v.style.setProperty('top', '0', 'important');
          v.style.setProperty('left', '0', 'important');
          v.style.setProperty('width', '100vw', 'important');
          v.style.setProperty('height', '100vh', 'important');
          v.style.setProperty('max-width', '100vw', 'important');
          v.style.setProperty('max-height', '100vh', 'important');
          v.style.setProperty('object-fit', 'contain', 'important');
          v.style.setProperty('margin', '0', 'important');
          v.style.setProperty('padding', '0', 'important');
          v.style.setProperty('z-index', '1', 'important');
        }
      }
    } catch (err) {
      // Best-effort guard
    }
  }

  // Execute immediately
  purgeUI();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', purgeUI);
  }
  window.addEventListener('load', purgeUI);

  // 4. Continuous Heartbeat Re-Injection (Anti-DOM Wipe) via persistent MutationObserver
  try {
    const observer = new MutationObserver(function() {
      purgeUI();
    });
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class', 'style']
    });
    window.__cleanfeed_observer = observer;
  } catch (e) {
    // Fallback if observer fails
  }

  // Periodic fallback heartbeat
  setInterval(purgeUI, 500);
})();
