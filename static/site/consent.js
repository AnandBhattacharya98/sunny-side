/* Sunny Side cookie consent.
 *
 * Sunny Side sets one essential cookie (the sign-in session) and no analytics or advertising
 * cookies at all. The only optional thing is "preferences": your theme, the finished tour,
 * Sunny's language and tips, and the practice phone number, kept in this browser's
 * localStorage. Until you allow preferences, those are kept for this tab only (sessionStorage)
 * and nothing new is written to localStorage. Your choice lives in the essential ss_consent
 * cookie for a year, then we ask again (or sooner when the policy version changes).
 *
 * Load this file first in <head>, without defer: the theme script right after it reads
 * localStorage through the guard below.
 */
(function () {
  'use strict';
  var script = document.currentScript;
  var VERSION = String((script && script.getAttribute('data-version')) || '1');
  var COOKIE = 'ss_consent';
  var YEAR = 365 * 24 * 60 * 60;
  var PREF_EXACT = { 'theme': 1, 'walkthrough_completed': 1, 'ss-practice-phone': 1, 'qz_handsfree': 1 };

  function isPref(key) {
    key = String(key);
    return PREF_EXACT.hasOwnProperty(key) || key.indexOf('va_') === 0;
  }

  function readChoice() {
    var m = document.cookie.match(/(?:^|;\s*)ss_consent=([^;]*)/);
    if (!m) return null;
    // v<version>.p<0|1>.<unix seconds>
    var parts = decodeURIComponent(m[1]).split('.');
    if (parts.length < 3 || parts[0] !== 'v' + VERSION) return null;
    return { preferences: parts[1] === 'p1', at: parseInt(parts[2], 10) || 0 };
  }

  var choice = readChoice();

  // ── Storage guard: preference keys only reach localStorage once preferences are allowed ──
  var proto = window.Storage && window.Storage.prototype;
  var raw = proto ? { get: proto.getItem, set: proto.setItem, remove: proto.removeItem } : null;
  var local = null, tab = null;
  try { local = window.localStorage; } catch (e) {}
  try { tab = window.sessionStorage; } catch (e) {}

  function prefsAllowed() { return !!(choice && choice.preferences); }

  if (raw && local && tab) {
    proto.getItem = function (key) {
      if (this === local && isPref(key) && !prefsAllowed()) {
        var v = raw.get.call(tab, 'ss_pref:' + key);
        // No choice made yet: still honour what an earlier visit saved, but don't add to it
        return v !== null ? v : (choice ? null : raw.get.call(local, key));
      }
      return raw.get.call(this, key);
    };
    proto.setItem = function (key, value) {
      if (this === local && isPref(key) && !prefsAllowed()) return raw.set.call(tab, 'ss_pref:' + key, value);
      return raw.set.call(this, key, value);
    };
    proto.removeItem = function (key) {
      if (this === local && isPref(key)) raw.remove.call(tab, 'ss_pref:' + key);
      return raw.remove.call(this, key);
    };
  }

  function eachKey(store, fn) {
    var keys = [];
    try { for (var i = 0; i < store.length; i++) keys.push(store.key(i)); } catch (e) { return; }
    keys.forEach(fn);
  }

  function moveStorage(allow) {
    if (!raw || !local || !tab) return;
    try {
      if (allow) {
        eachKey(tab, function (k) {
          if (k && k.indexOf('ss_pref:') === 0) {
            raw.set.call(local, k.slice(8), raw.get.call(tab, k));
            raw.remove.call(tab, k);
          }
        });
      } else {
        eachKey(local, function (k) {
          if (k && isPref(k)) {
            raw.set.call(tab, 'ss_pref:' + k, raw.get.call(local, k));
            raw.remove.call(local, k);
          }
        });
      }
    } catch (e) {}
  }

  function save(preferences) {
    var now = Math.floor(Date.now() / 1000);
    var value = 'v' + VERSION + '.' + (preferences ? 'p1' : 'p0') + '.' + now;
    document.cookie = COOKIE + '=' + value + '; Max-Age=' + YEAR + '; Path=/; SameSite=Lax' +
      (location.protocol === 'https:' ? '; Secure' : '');
    choice = { preferences: !!preferences, at: now };
    moveStorage(!!preferences);
    try { window.dispatchEvent(new CustomEvent('sunny:consent', { detail: { preferences: !!preferences } })); } catch (e) {}
  }

  window.SunnyConsent = {
    get: function () { return choice ? { essential: true, preferences: choice.preferences, at: choice.at } : null; },
    allows: function (category) { return category === 'essential' || (category === 'preferences' && prefsAllowed()); },
    save: save,
    open: function () { openPanel(); }
  };

  // ── Banner and preferences panel ──
  var banner = null, panel = null, lastFocus = null;

  function el(html) {
    var t = document.createElement('div');
    t.innerHTML = html.trim();
    return t.firstChild;
  }

  function hideBanner() {
    if (!banner) return;
    var b = banner;
    banner = null;
    b.classList.remove('in');
    setTimeout(function () { if (b.parentNode) b.parentNode.removeChild(b); }, 320);
  }

  function showBanner() {
    if (banner || !document.body) return;
    banner = el(
      '<section class="ss-cc" role="region" aria-label="Cookie choices">' +
        '<div class="ss-cc-text">' +
          '<strong>Cookies, kept to a minimum.</strong> ' +
          'We use one essential cookie to keep you signed in and safe. With your OK we also remember ' +
          'preferences on this device, like your theme and dismissed tips. No ads, no analytics, no tracking. ' +
          '<a href="/cookies">Cookie Policy</a>' +
        '</div>' +
        '<div class="ss-cc-actions">' +
          '<button type="button" class="ss-cc-btn" data-cc="choose">Choose</button>' +
          '<button type="button" class="ss-cc-btn" data-cc="essential">Essential only</button>' +
          '<button type="button" class="ss-cc-btn ss-cc-primary" data-cc="all">Accept all</button>' +
        '</div>' +
      '</section>');
    banner.addEventListener('click', function (e) {
      var b = e.target.closest('[data-cc]');
      if (!b) return;
      var what = b.getAttribute('data-cc');
      if (what === 'choose') { openPanel(); return; }
      save(what === 'all');
      hideBanner();
    });
    document.body.appendChild(banner);
    requestAnimationFrame(function () { requestAnimationFrame(function () { if (banner) banner.classList.add('in'); }); });
  }

  function closePanel() {
    if (!panel) return;
    var p = panel;
    panel = null;
    p.classList.remove('in');
    document.removeEventListener('keydown', onKey, true);
    setTimeout(function () { if (p.parentNode) p.parentNode.removeChild(p); }, 260);
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
  }

  function onKey(e) {
    if (!panel) return;
    if (e.key === 'Escape') { e.preventDefault(); closePanel(); return; }
    if (e.key === 'Tab') {
      var f = panel.querySelectorAll('button, input, a[href]');
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function row(id, title, body, state) {
    var input = state === 'locked'
      ? '<span class="ss-cc-tag">Always on</span>'
      : state === 'none'
        ? '<span class="ss-cc-tag ss-cc-tag-off">Not used</span>'
        : '<label class="ss-cc-switch"><input type="checkbox" id="' + id + '"' + (state ? ' checked' : '') +
          ' aria-describedby="' + id + '-d"><span aria-hidden="true"></span><span class="ss-cc-sr">' + title + '</span></label>';
    return '<div class="ss-cc-row"><div><div class="ss-cc-row-t">' + title + '</div>' +
      '<p id="' + id + '-d">' + body + '</p></div>' + input + '</div>';
  }

  function openPanel() {
    if (panel || !document.body) return;
    lastFocus = document.activeElement;
    var on = prefsAllowed();
    panel = el(
      '<div class="ss-cc-scrim">' +
        '<div class="ss-cc-panel" role="dialog" aria-modal="true" aria-labelledby="ss-cc-h">' +
          '<h2 id="ss-cc-h">Cookie preferences</h2>' +
          '<p class="ss-cc-lead">Choose what Sunny Side may keep in this browser. You can change this any time from ' +
            '"Cookie settings" at the bottom of every page.</p>' +
          row('ss-cc-ess', 'Essential', 'Keeps you signed in, protects forms from forgery and remembers this choice. ' +
            'The site cannot work without them.', 'locked') +
          row('ss-cc-pref', 'Preferences', 'Remembers your light or dark theme, that you finished the tour, Sunny\'s ' +
            'language and voice settings, dismissed tips and your practice phone number, on this device only.', on) +
          row('ss-cc-ana', 'Analytics &amp; advertising', 'We don\'t use any. No third-party trackers, pixels or ad cookies.', 'none') +
          '<div class="ss-cc-panel-actions">' +
            '<a href="/cookies">Read the Cookie Policy</a>' +
            '<span class="ss-cc-grow"></span>' +
            '<button type="button" class="ss-cc-btn" data-cc="essential">Essential only</button>' +
            '<button type="button" class="ss-cc-btn ss-cc-primary" data-cc="save">Save choices</button>' +
          '</div>' +
        '</div>' +
      '</div>');
    panel.addEventListener('click', function (e) {
      if (e.target === panel) { closePanel(); return; }
      var b = e.target.closest('[data-cc]');
      if (!b) return;
      var what = b.getAttribute('data-cc');
      var pref = what === 'save' ? !!panel.querySelector('#ss-cc-pref').checked : false;
      save(pref);
      hideBanner();
      closePanel();
      announce(pref ? 'Saved. Preferences will be remembered on this device.' : 'Saved. Only essential cookies are used.');
    });
    document.body.appendChild(panel);
    document.addEventListener('keydown', onKey, true);
    requestAnimationFrame(function () { requestAnimationFrame(function () { if (panel) panel.classList.add('in'); }); });
    var focusTarget = panel.querySelector('#ss-cc-pref');
    if (focusTarget) focusTarget.focus();
  }

  function announce(text) {
    var live = document.getElementById('ss-cc-live');
    if (!live) {
      live = el('<div id="ss-cc-live" class="ss-cc-toast" role="status" aria-live="polite"></div>');
      document.body.appendChild(live);
    }
    live.textContent = text;
    live.classList.add('in');
    clearTimeout(announce.t);
    announce.t = setTimeout(function () { live.classList.remove('in'); }, 2600);
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-cookie-settings]');
    if (t) { e.preventDefault(); openPanel(); }
  });

  function boot() {
    if (!choice) showBanner();
    if (location.hash === '#cookie-settings') openPanel();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
