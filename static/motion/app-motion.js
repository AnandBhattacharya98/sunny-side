/* Sunny Side motion layer for the signed-in app, sign-in and onboarding pages.
   Uses window.SunnyAnim (static/vendor/sunny-anim.min.js: Anime.js, AutoAnimate,
   canvas-confetti) when it has loaded, and plain CSS otherwise. Every effect checks
   the device's "reduce motion" setting first. */
(function () {
  'use strict';
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function calm() { return !!(mq && mq.matches); }
  function lib() { return window.SunnyAnim || null; }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  var BRAND = ['#FFA700', '#FFB938', '#FF6B3D', '#0D0C0A', '#F5F2EB'];

  /* ---------- Stagger: number each item so CSS can delay it ---------- */
  function number(selector, max, parentSel) {
    var groups = parentSel ? $all(parentSel) : [document];
    groups.forEach(function (g) {
      $all(selector, g).forEach(function (el, i) { el.style.setProperty('--i', Math.min(i, max)); });
    });
  }
  function stagger() {
    $all('.board > .col').forEach(function (col, ci) {
      col.style.setProperty('--ci', Math.min(ci, 7));
      $all('.jcard', col).forEach(function (card, i) {
        card.style.setProperty('--ci', Math.min(ci, 7));
        card.style.setProperty('--i', Math.min(i, 8));
      });
    });
    number('.mcard-feed', 12);
    number('.pg-kpi', 4);
    number('.pg-card', 6);
    number('.pg-frow .pg-fbar > span', 6);
    number('.pg-week .pg-wbar', 16);
    number('.pg-table .pg-rate > span', 10);
    $all('.auth-card').forEach(function (card) {
      var i = 0;
      $all('.auth-head > *, .auth-form > *, :scope > .auth-or, :scope > .auth-switch', card).forEach(function (el) {
        el.style.setProperty('--i', Math.min(i++, 14));
      });
    });
  }

  /* ---------- Count numbers up from zero ---------- */
  function countUp(el, duration) {
    var raw = (el.textContent || '').trim();
    var m = raw.match(/^(\d+(?:\.\d+)?)(%?)$/);
    if (!m || calm()) return;
    var target = parseFloat(m[1]), decimals = (m[1].split('.')[1] || '').length, suffix = m[2];
    if (!target) return;
    var start = null, written = raw;
    el.dataset.mCounting = '1';
    function tick(t) {
      if (start === null) start = t;
      var k = Math.min((t - start) / (duration || 800), 1), eased = 1 - Math.pow(1 - k, 3);
      // Stop if something else (a filter, a refresh) has rewritten the number meanwhile
      if (el.textContent !== written) { delete el.dataset.mCounting; return; }
      written = k < 1 ? (target * eased).toFixed(decimals) + suffix : raw;
      el.textContent = written;
      if (k < 1) requestAnimationFrame(tick); else delete el.dataset.mCounting;
    }
    requestAnimationFrame(tick);
  }

  /* ---------- A little bounce whenever a count changes ---------- */
  function watchCounts() {
    if (!('MutationObserver' in window)) return;
    $all('.col-ct, .sidebar .nc').forEach(function (el) {
      var last = el.textContent;
      new MutationObserver(function () {
        if (el.dataset.mCounting === '1') return;
        if (el.textContent === last) return;
        last = el.textContent;
        if (calm()) return;
        el.classList.remove('m-bump'); void el.offsetWidth; el.classList.add('m-bump');
      }).observe(el, { childList: true, characterData: true, subtree: true });
    });
  }

  /* ---------- Ripple where the button was pressed ---------- */
  var RIPPLE = '.btn, .auth-btn, .fj-go, .fu-btn, .tab-btn';
  function ripple(e) {
    if (calm() || e.button > 0) return;
    var btn = e.target.closest && e.target.closest(RIPPLE);
    if (!btn || btn.disabled) return;
    var cs = getComputedStyle(btn);
    if (cs.position === 'static') btn.style.position = 'relative';
    if (cs.overflow !== 'hidden') btn.style.overflow = 'hidden';
    var r = btn.getBoundingClientRect(), size = Math.max(r.width, r.height) * 2.2;
    var dot = document.createElement('span');
    dot.className = 'm-ripple';
    dot.style.width = dot.style.height = size + 'px';
    dot.style.left = (e.clientX - r.left - size / 2) + 'px';
    dot.style.top = (e.clientY - r.top - size / 2) + 'px';
    btn.appendChild(dot);
    setTimeout(function () { dot.remove(); }, 600);
  }

  /* ---------- Confetti ---------- */
  function celebrate(opts) {
    var A = lib();
    if (!A || !A.confetti || calm()) return;
    opts = opts || {};
    var origin = opts.origin || { x: 0.5, y: 0.6 };
    var base = { colors: BRAND, disableForReducedMotion: true, zIndex: 30000, ticks: 220 };
    A.confetti(Object.assign({}, base, { particleCount: opts.count || 90, spread: 70, startVelocity: 42, origin: origin }));
    setTimeout(function () {
      A.confetti(Object.assign({}, base, { particleCount: Math.round((opts.count || 90) / 2), spread: 110, startVelocity: 30, scalar: 0.8, origin: origin }));
    }, 180);
  }

  /* ---------- Board: fly a moved card into its new column ---------- */
  var pendingMove = null, glides = [];
  function flyCard(jobId, col) {
    var card = document.querySelector('.jcard[data-job-id="' + (window.CSS && CSS.escape ? CSS.escape(jobId) : jobId) + '"]');
    var dest = document.querySelector('#col-' + col + ' .col-cards');
    if (!card || !dest || card.parentElement === dest) return card;
    var from = card.closest('.col');
    var first = card.getBoundingClientRect();
    // AutoAnimate would also try to animate this move; this one is ours
    glides.forEach(function (g) { try { g.disable(); } catch (e) {} });
    dest.insertBefore(card, dest.firstChild);
    var last = card.getBoundingClientRect();
    resumeGlides();
    [from, dest.closest('.col')].forEach(function (c, i) {
      var ct = c && c.querySelector('.col-ct');
      if (ct && /^\d+$/.test(ct.textContent.trim())) ct.textContent = Math.max(0, parseInt(ct.textContent, 10) + (i ? 1 : -1));
    });
    if (!calm() && card.animate) {
      card.style.animation = 'none';
      card.animate([
        { transform: 'translate(' + (first.left - last.left) + 'px,' + (first.top - last.top) + 'px) rotate(-3deg) scale(1.04)', boxShadow: '0 24px 40px -18px rgba(0,0,0,.45)' },
        { transform: 'none', boxShadow: '0 0 0 0 transparent' }
      ], { duration: 620, easing: 'cubic-bezier(.34,1.3,.64,1)' });
    }
    card.classList.remove('m-landed'); void card.offsetWidth; card.classList.add('m-landed');
    return card;
  }
  function hookBoard() {
    if (typeof window.moveJob === 'function' && !window.moveJob.mWrapped) {
      var orig = window.moveJob;
      window.moveJob = function (jobId, col) {
        pendingMove = { jobId: jobId, col: col };
        return orig.apply(this, arguments);
      };
      window.moveJob.mWrapped = true;
    }
    // Sunny reacts only once the server has saved the move, so the card flies then
    if (typeof window.sunnyReactToMove === 'function' && !window.sunnyReactToMove.mWrapped) {
      var react = window.sunnyReactToMove;
      window.sunnyReactToMove = function (status) {
        var mv = pendingMove; pendingMove = null;
        try {
          if (mv) {
            var card = flyCard(mv.jobId, mv.col);
            if (status === 'offer') {
              var r = card && card.getBoundingClientRect();
              celebrate({ count: 140, origin: r ? { x: (r.left + r.width / 2) / innerWidth, y: (r.top + r.height / 2) / innerHeight } : undefined });
            }
          }
        } catch (e) { /* motion must never block the move itself */ }
        return react.apply(this, arguments);
      };
      window.sunnyReactToMove.mWrapped = true;
    }
    // Sorting and newly added cards glide into place
    var A = lib();
    if (A && A.autoAnimate && !calm()) {
      $all('.col-cards, #fj-list, #fu-body, .fj-co-list').forEach(function (el) {
        try { glides.push(A.autoAnimate(el, { duration: 260, easing: 'cubic-bezier(.2,.7,.2,1)' })); } catch (e) {}
      });
    }
  }

  function resumeGlides() {
    // Re-enable after this frame's mutation records have been delivered
    setTimeout(function () { glides.forEach(function (g) { try { g.enable(); } catch (e) {} }); }, 0);
  }

  /* ---------- Theme switch: the new theme spreads out from the button ---------- */
  var lastPoint = null;
  function hookTheme() {
    document.addEventListener('pointerdown', function (e) { lastPoint = { x: e.clientX, y: e.clientY }; }, true);
    if (typeof window.toggleTheme !== 'function' || window.toggleTheme.mWrapped) return;
    var orig = window.toggleTheme;
    window.toggleTheme = function () {
      var self = this, args = arguments;
      if (calm() || !document.startViewTransition) return orig.apply(self, args);
      var p = lastPoint || { x: innerWidth / 2, y: innerHeight / 2 };
      var radius = Math.hypot(Math.max(p.x, innerWidth - p.x), Math.max(p.y, innerHeight - p.y));
      var root = document.documentElement;
      root.classList.add('ss-theme-vt');
      var vt;
      try { vt = document.startViewTransition(function () { orig.apply(self, args); }); }
      catch (e) { root.classList.remove('ss-theme-vt'); return orig.apply(self, args); }
      vt.ready.then(function () {
        root.animate({ clipPath: ['circle(0px at ' + p.x + 'px ' + p.y + 'px)', 'circle(' + radius + 'px at ' + p.x + 'px ' + p.y + 'px)'] },
                     { duration: 560, easing: 'cubic-bezier(.2,.7,.2,1)', pseudoElement: '::view-transition-new(root)' });
      }).catch(function () {});
      vt.finished.finally(function () { root.classList.remove('ss-theme-vt'); });
    };
    window.toggleTheme.mWrapped = true;
  }

  /* ---------- Onboarding: a small cheer when the first matches appear ---------- */
  function hookOnboarding() {
    var step = document.getElementById('onboard-suggested-step');
    if (!step || !('MutationObserver' in window)) return;
    var done = false;
    new MutationObserver(function () {
      if (done || step.style.display === 'none') return;
      done = true;
      setTimeout(function () { celebrate({ count: 70, origin: { x: 0.5, y: 0.3 } }); }, 250);
    }).observe(step, { attributes: true, attributeFilter: ['style'] });
  }

  /* ---------- Lists filled in later (Today's Picks, follow-ups) get numbered as they arrive ---------- */
  function watchLists() {
    if (!('MutationObserver' in window)) return;
    ['daily-picks-body', 'fu-body'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      new MutationObserver(function () {
        Array.prototype.forEach.call(el.children, function (c, i) { c.style.setProperty('--i', Math.min(i, 8)); });
      }).observe(el, { childList: true });
    });
  }

  /* ---------- Log out without a page transition ----------
     /logout answers with a redirect, and some browsers can stall a page transition
     across a redirect. Turn the transition off just for this navigation. */
  var leavingForLogout = false;
  function hookLogout() {
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href="/logout"]');
      if (!a) return;
      leavingForLogout = true;
      var off = document.createElement('style');
      off.textContent = '@view-transition { navigation: none; }';
      document.head.appendChild(off);
    }, true);
    window.addEventListener('pageswap', function (e) {
      if (leavingForLogout && e.viewTransition) { try { e.viewTransition.skipTransition(); } catch (err) {} }
    });
  }

  function init() {
    document.documentElement.classList.add('m-ready');
    stagger();
    // Count the numbers that summarise a page (columns, progress tiles)
    setTimeout(function () {
      $all('.col-ct, .pg-kpi-n').forEach(function (el) { countUp(el, 900); });
    }, 250);
    watchCounts();
    watchLists();
    hookBoard();
    hookTheme();
    hookOnboarding();
    hookLogout();
    document.addEventListener('pointerdown', ripple, { passive: true });
  }

  window.SunnyMotion = { calm: calm, celebrate: celebrate, countUp: countUp, flyCard: flyCard };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
