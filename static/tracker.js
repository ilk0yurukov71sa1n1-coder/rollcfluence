/* Rollcfluence page tracker.
   One source of truth, delivered two ways:

     1. Pasted inline, inside a script tag at the end of the head of a
        Rollcfluence booking page. (No closing script tag is written anywhere in
        this file — an HTML parser would end the block early and break the page.)
        It then learns the business slug by watching
        the /api/register call the page already makes, and detects a booking by
        watching /api/book/. Zero configuration.

     2. Served as a file, via an async script tag pointing at
        https://rollcfluence.onrender.com/t.js?s=the-slug ,
        on any other website. The slug comes from the script's own URL, the API
        base from the script's own origin, and `async` guarantees it can never
        slow the host page down — which matters when it is somebody else's site.

   What it records: views, clicks and taps (as a percentage of the page, so
   every screen size lands on the same heatmap), rage-clicks, which form field
   was focused, scroll depth, a 20-second heartbeat while the tab is visible,
   and how long the visit lasted.

   What it never records: cookies, IP addresses, user agents, or anything typed
   into a form — only *which* field was touched, never its contents. That is
   deliberate: it keeps the data anonymous, which is why Rollcfluence clients
   need no cookie banner. */
(function () {
  "use strict";
  if (window.__rcTracker) return;              // pasted twice? do nothing twice.
  window.__rcTracker = true;

  var API = "https://rollcfluence.onrender.com";
  var slug = null;

  // When loaded as a file, take both the slug and the backend address from the
  // script tag itself. document.currentScript is only valid during this first
  // synchronous run, which is exactly where we are.
  try {
    var me = document.currentScript;
    if (me && me.src) {
      var q = me.src.match(/[?&]s=([^&#]+)/);
      if (q) slug = decodeURIComponent(q[1]);
      var origin = me.src.split("/").slice(0, 3).join("/");
      if (/^https?:/.test(origin)) API = origin;
    }
  } catch (e) { /* never let setup break a client's page */ }
  if (!slug && window.RC_SLUG) slug = String(window.RC_SLUG);

  var sid = Math.random().toString(36).slice(2) + Date.now().toString(36),
      t0 = Date.now(),
      queue = [],
      sent = 0,
      beats = 0,
      maxScroll = 0,
      exited = false;

  function now() { return Date.now() - t0; }

  function track(type, extra) {
    if (sent > 300) return;                    // hard cap, mirrors the server's
    var ev = { type: type, ms: now() };
    if (extra) for (var k in extra) ev[k] = extra[k];
    queue.push(ev);
    if (queue.length >= 10) flush();
  }

  function flush() {
    if (!slug) return;                         // hold events until the slug is known
    while (queue.length) {
      var batch = queue.slice(0, 60);
      queue = queue.slice(60);
      sent += batch.length;
      var body = JSON.stringify({
        sid: sid, w: innerWidth, h: innerHeight,
        ref: document.referrer, events: batch
      });
      // text/plain on purpose: any other content type makes the browser send a
      // CORS preflight, and preflights fired while the tab is closing get lost.
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon(API + "/api/track/" + slug, body);
        } else {
          realFetch.call(window, API + "/api/track/" + slug,
                         { method: "POST", body: body, keepalive: true });
        }
      } catch (e) { /* analytics must never break the page */ }
    }
  }

  // ── Learn the slug, and catch the booking, by watching the calls a
  //    Rollcfluence page already makes. This is what makes the inline paste
  //    configuration-free. On a third-party site none of this ever fires, and
  //    the slug simply came from the script URL instead.
  var realFetch = window.fetch;
  if (typeof realFetch === "function") {
    window.fetch = function (input) {
      var url = (typeof input === "string" ? input : (input && input.url)) || "";
      var isBook = url.indexOf("/api/book/") !== -1;
      if (isBook) track("submit");
      return realFetch.apply(this, arguments).then(function (res) {
        try {
          if (url.indexOf("/api/register") !== -1) {
            res.clone().json().then(function (d) {
              if (d && d.slug) { slug = d.slug; flush(); }
            }).catch(function () {});
          } else if (isBook) {
            res.clone().json().then(function (d) {
              if (d && d.ok) { track("success"); flush(); }
            }).catch(function () {});
          }
        } catch (e) {}
        return res;
      });
    };
  }

  // ── Clicks and taps ───────────────────────────────────────────────
  // Positions are stored as a percentage of the page rather than in pixels, so
  // a phone and a laptop land on the same heatmap.
  var rageAt = null, rageN = 0, rageT = 0;

  addEventListener("click", function (e) {
    var t = e.target || {};
    var name = t.id || t.name || (t.getAttribute && t.getAttribute("data-track")) ||
               (t.tagName === "BUTTON" || t.tagName === "A" ? (t.textContent || "").trim().slice(0, 30) : "") ||
               (t.tagName || "").toLowerCase();
    var pageH = Math.max(document.body ? document.body.scrollHeight : 0, innerHeight);
    var x = +(e.clientX / innerWidth * 100).toFixed(2);
    var y = +((e.clientY + (window.scrollY || 0)) / pageH * 100).toFixed(2);
    var el = String(name).slice(0, 40);
    track("click", { x: x, y: y, el: el });

    // Three clicks inside one second, within roughly 40px, on the same target:
    // that is somebody jabbing at something that is not responding.
    var ts = Date.now();
    if (rageAt && ts - rageT < 1000 &&
        Math.abs(rageAt.x - x) < 4 && Math.abs(rageAt.y - y) < 4) {
      rageN++;
      if (rageN === 3) track("rage", { x: x, y: y, el: el });
    } else {
      rageN = 1;
    }
    rageAt = { x: x, y: y }; rageT = ts;
  }, true);

  // ── Which field they touched (never what they typed) ───────────────
  function watchFields() {
    var fields = document.querySelectorAll("input, select, textarea");
    for (var i = 0; i < fields.length; i++) {
      (function (f) {
        if (f.__rcWatched) return;
        f.__rcWatched = true;
        f.addEventListener("focus", function () {
          track("focus", { el: (f.name || f.id || f.type || "field").slice(0, 40) });
        }, { once: true });
      })(fields[i]);
    }
  }
  watchFields();
  // Fields that appear later (a 3D card flipping over, a step-2 panel) too.
  if (window.MutationObserver) {
    new MutationObserver(watchFields).observe(document.documentElement,
                                              { childList: true, subtree: true });
  }

  addEventListener("scroll", function () {
    var pageH = Math.max((document.body ? document.body.scrollHeight : 0) - innerHeight, 1);
    var depth = Math.min(100, (window.scrollY || 0) / pageH * 100);
    if (depth > maxScroll + 5) maxScroll = depth;
  }, { passive: true });

  // ── Heartbeat: the pulse behind "live now" ─────────────────────────
  // Only while the tab is actually visible, so a page left open in a
  // background tab never counts as somebody standing in the shop.
  setInterval(function () {
    if (document.visibilityState !== "visible") return;
    if (beats++ >= 90) return;                 // stop after ~30 minutes
    track("hb");
    flush();
  }, 20000);

  // ── The tail of the visit ──────────────────────────────────────────
  // Re-armed when the visitor comes back to the tab, so a phone call in the
  // middle of booking does not end the recording.
  function sendExit() {
    if (exited) return;
    exited = true;
    if (maxScroll > 0) queue.push({ type: "scroll", y: +maxScroll.toFixed(1), ms: now() });
    queue.push({ type: "exit", ms: now() });
    flush();
  }
  addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") sendExit();
    else exited = false;
  });
  addEventListener("pagehide", sendExit);

  // Pages that set the slug themselves rather than letting the fetch wrapper
  // discover it (a 3D page whose register call happens late, for instance).
  var watch = setInterval(function () {
    if (!slug && window.RC_SLUG) { slug = String(window.RC_SLUG); flush(); }
    if (slug) clearInterval(watch);
  }, 400);
  setTimeout(function () { clearInterval(watch); }, 30000);

  track("view");
  setTimeout(flush, 4000);            // don't wait for 10 events on a quiet visit
})();
