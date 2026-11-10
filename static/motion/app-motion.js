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

  /* ---------- Sign in and sign up: Sunny keeps you company ----------
     Its eyes follow the cursor and whichever box you're typing in, it covers its eyes
     while you type a password (and peeks if you press Show), frowns at a wrong password
     and cheers when you submit. */
  var SUNNY_SVG = function () {
    var rays = '';
    for (var a = 0; a < 360; a += 45) rays += '<line x1="50" y1="3" x2="50" y2="14" transform="rotate(' + a + ' 50 50)"/>';
    return '<svg class="sunny" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" focusable="false">' +
      '<g class="s-rays" stroke="#FFA700" stroke-width="7" stroke-linecap="round">' + rays + '</g>' +
      '<g class="s-body">' +
        '<circle cx="50" cy="50" r="31" fill="#FFA700"/>' +
        '<circle cx="50" cy="50" r="31" fill="none" stroke="#0D0C0A" stroke-width="2.5"/>' +
        '<ellipse cx="34" cy="58" rx="5.5" ry="3.2" fill="#FF6B3D" opacity="0.5"/>' +
        '<ellipse cx="66" cy="58" rx="5.5" ry="3.2" fill="#FF6B3D" opacity="0.5"/>' +
        '<g class="s-eyes">' +
          '<ellipse class="s-eye" cx="40" cy="46" rx="3.8" ry="5" fill="#0D0C0A"/>' +
          '<ellipse class="s-eye" cx="60" cy="46" rx="3.8" ry="5" fill="#0D0C0A"/>' +
          '<circle class="s-eye" cx="41.3" cy="44" r="1.3" fill="#FFFDF8"/>' +
          '<circle class="s-eye" cx="61.3" cy="44" r="1.3" fill="#FFFDF8"/>' +
        '</g>' +
        '<g class="s-shut" stroke="#0D0C0A" stroke-width="3" stroke-linecap="round" fill="none">' +
          '<path d="M35.5 45 Q40 50 44.5 45"/><path d="M55.5 45 Q60 50 64.5 45"/></g>' +
        '<g class="s-joy" stroke="#0D0C0A" stroke-width="3" stroke-linecap="round" fill="none">' +
          '<path d="M35.5 47 Q40 41 44.5 47"/><path d="M55.5 47 Q60 41 64.5 47"/></g>' +
        '<path class="s-smile" d="M41 58 Q50 67 59 58" stroke="#0D0C0A" stroke-width="3" stroke-linecap="round" fill="none"/>' +
        '<g class="s-hands" fill="#FFC24D" stroke="#0D0C0A" stroke-width="2.2">' +
          '<path d="M27 52 Q26 38 38 37 Q49 37 48 49 Q47 55 38 55 Q30 56 27 52 Z"/>' +
          '<path d="M73 52 Q74 38 62 37 Q51 37 52 49 Q53 55 62 55 Q70 56 73 52 Z"/>' +
          '<path d="M33 40 L35 45 M38 38 L39 44 M43 39 L43 45 M67 40 L65 45 M62 38 L61 44 M57 39 L57 45" fill="none" stroke-width="1.6" stroke-linecap="round"/></g>' +
      '</g></svg>';
  };
  var AUTH_COPY = {
    login: { hi: 'Welcome back!', chips: ['<span class="ico">✉</span>3 new matches <small>today</small>', 'Fit score <span class="tag">8.4</span>', '<span class="ico">☎</span>Interview <small>Tue, 3 pm</small>'] },
    signup: { hi: "Let's find your next job", chips: ['<span class="ico">✎</span>Cover letter drafted', 'Fit score <span class="tag">8.4</span>', '<span class="ico">✉</span>Swiggy replied <small>2m ago</small>'] },
    forgot: { hi: 'Happens to everyone', chips: ['<span class="ico">✉</span>Reset link <small>on its way</small>', 'Your board <span class="tag">safe</span>'] },
    reset: { hi: 'Pick a good one', chips: ['<span class="ico">🔒</span>8+ characters', 'Your board <span class="tag">safe</span>'] }
  };
  function hookAuthSun() {
    var aside = document.getElementById('auth-sun');
    if (!aside) return;
    var shell = aside.closest('.auth-shell');
    var copy = AUTH_COPY[aside.dataset.mode] || AUTH_COPY.login;
    var html = '<div class="as-wrap"><div class="as-burst"></div><div class="as-halo"></div>' + SUNNY_SVG() + '</div>';
    copy.chips.forEach(function (c, i) { html += '<div class="as-chip c' + (i + 1) + '"><span>' + c + '</span></div>'; });
    html += '<div class="as-bubble" id="as-bubble">' + copy.hi + '</div>';
    aside.innerHTML = html;
    // The same spinning sun logo as the homepage
    var dot = shell && shell.querySelector('.auth-logo-dot');
    if (dot) {
      var rays = '';
      [[12,1,12,4],[12,20,12,23],[1,12,4,12],[20,12,23,12],[4.2,4.2,6.3,6.3],[17.7,17.7,19.8,19.8],[4.2,19.8,6.3,17.7],[17.7,6.3,19.8,4.2]].forEach(function (l) {
        rays += '<line x1="' + l[0] + '" y1="' + l[1] + '" x2="' + l[2] + '" y2="' + l[3] + '"/>';
      });
      var logo = document.createElement('span');
      logo.className = 'auth-logo-sun';
      logo.setAttribute('aria-hidden', 'true');
      logo.innerHTML = '<svg viewBox="0 0 24 24"><g class="rays" stroke="#FFA700" stroke-width="2.2" stroke-linecap="round">' + rays + '</g><circle cx="12" cy="12" r="5.5" fill="#FFA700"/></svg>';
      dot.replaceWith(logo);
    }
    if (shell && !shell.querySelector('.auth-glow')) {
      var glow = document.createElement('div');
      glow.className = 'auth-glow';
      glow.setAttribute('aria-hidden', 'true');
      shell.insertBefore(glow, shell.firstChild);
    }
    var wrap = aside.querySelector('.as-wrap'), eyes = aside.querySelector('.s-eyes'), bubble = aside.querySelector('#as-bubble');
    var moodTimer = null;
    function say(text) { if (bubble && text) { bubble.textContent = text; bubble.style.animation = 'none'; void bubble.offsetWidth; bubble.style.animation = ''; } }
    function mood(name, ms, text) {
      aside.classList.remove('happy', 'sad', 'cheer', 'nod'); void aside.offsetWidth;
      aside.classList.add(name);
      say(text);
      clearTimeout(moodTimer);
      if (ms) moodTimer = setTimeout(function () { aside.classList.remove(name); }, ms);
    }
    function lookAt(x, y) {
      var r = wrap.getBoundingClientRect();
      var dx = x - (r.left + r.width / 2), dy = y - (r.top + r.height * .46);
      var d = Math.hypot(dx, dy) || 1, k = Math.min(d / 300, 1) * 4.5;
      eyes.style.transform = 'translate(' + (dx / d * k).toFixed(2) + 'px,' + (dy / d * k).toFixed(2) + 'px)';
    }
    // A wrong password: Sunny looks sad for a moment
    if (document.querySelector('.auth-card .auth-error')) setTimeout(function () { mood('sad', 2600, 'Hmm, try again?'); }, 500);

    if (!calm()) {
      var queued = null;
      window.addEventListener('pointermove', function (e) {
        if (queued) return;
        queued = requestAnimationFrame(function () {
          queued = null;
          lookAt(e.clientX, e.clientY);
          if (shell) {
            var sr = shell.getBoundingClientRect();
            shell.style.setProperty('--px', (e.clientX - sr.left) + 'px');
            shell.style.setProperty('--py', (e.clientY - sr.top) + 'px');
          }
          aside.style.setProperty('--mx', ((e.clientX / innerWidth) - .5).toFixed(3));
          aside.style.setProperty('--my', ((e.clientY / innerHeight) - .5).toFixed(3));
        });
      }, { passive: true });
    }
    // Focus a box: look at it. A password box: cover the eyes.
    var form = document.querySelector('.auth-card');
    if (form) {
      form.addEventListener('focusin', function (e) {
        var el = e.target;
        if (!el.matches || !el.matches('input, textarea')) return;
        var r = el.getBoundingClientRect();
        lookAt(r.left + Math.min(r.width, 40 + (el.value || '').length * 7), r.top + r.height / 2);
        var secret = el.closest('.auth-pw') !== null;
        aside.classList.toggle('shy', secret);
        aside.classList.toggle('peek', secret && el.type === 'text');
        if (secret) say(el.type === 'text' ? 'Just a peek…' : "I'm not looking!");
        else say(copy.hi);
      });
      form.addEventListener('focusout', function () {
        setTimeout(function () {
          var a = document.activeElement;
          if (!a || !a.closest || !a.closest('.auth-pw')) { aside.classList.remove('shy', 'peek'); say(copy.hi); }
        }, 0);
      });
      form.addEventListener('input', function (e) {
        var el = e.target;
        if (el.closest && el.closest('.auth-pw')) return;
        var r = el.getBoundingClientRect();
        lookAt(r.left + Math.min(r.width - 10, 14 + (el.value || '').length * 8), r.top + r.height / 2);
        if (!calm()) { aside.classList.remove('nod'); void aside.offsetWidth; aside.classList.add('nod'); }
      });
      // "Show" toggles the password box between hidden and visible: peek or cover again
      form.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('.auth-show');
        if (!btn) return;
        setTimeout(function () {
          var input = btn.parentElement.querySelector('input');
          var showing = input && input.type === 'text';
          aside.classList.add('shy');
          aside.classList.toggle('peek', !!showing);
          say(showing ? 'Just a peek…' : "I'm not looking!");
        }, 0);
      });
      form.addEventListener('submit', function () { mood('cheer', 0, "Let's go!"); aside.classList.add('happy'); });
      Array.prototype.forEach.call(form.querySelectorAll('.auth-btn-p'), function (b) {
        b.addEventListener('mouseenter', function () { if (!aside.classList.contains('shy')) aside.classList.add('happy'); });
        b.addEventListener('mouseleave', function () { aside.classList.remove('happy'); });
      });
    }
    wrap.addEventListener('click', function () {
      mood('cheer', 900);
      var r = wrap.getBoundingClientRect();
      celebrate({ count: 50, origin: { x: (r.left + r.width / 2) / innerWidth, y: (r.top + r.height / 2) / innerHeight } });
    });
    // The heading's words rise into place
    var A = lib();
    var h1 = document.querySelector('.auth-card .auth-head h1');
    if (A && A.splitText && h1 && !calm()) {
      try {
        var words = A.splitText(h1, { words: { wrap: 'clip' } }).words;
        A.animate(words, { y: ['110%', '0%'], duration: 800, delay: A.stagger(60, { start: 200 }), ease: 'outExpo' });
      } catch (e) {}
    }
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
    hookAuthSun();
    document.addEventListener('pointerdown', ripple, { passive: true });
  }

  window.SunnyMotion = { calm: calm, celebrate: celebrate, countUp: countUp, flyCard: flyCard };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
