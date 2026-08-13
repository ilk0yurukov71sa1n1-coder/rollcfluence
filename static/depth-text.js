/*
 * Rollcfluence — DepthText
 *
 * Vanilla port of the React Bits <DepthText />. No dependencies at all: the
 * extrusion is just N stacked copies of the word at increasing negative
 * translateZ, each blended a step further from the face colour toward the
 * depth colour. Colours are interpolated in JS rather than with CSS
 * color-mix() so it renders identically on older browsers.
 *
 * Usage:
 *   <span data-depth-text="Book" data-face="#f8fafc" data-deep="#7c3aed"></span>
 */
(function () {
  function hexToRgb(h) {
    h = String(h).replace("#", "").trim();
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    var n = parseInt(h || "000000", 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function build(root) {
    var d = root.dataset;
    var text    = d.depthText || "Elevate";
    var layers  = Math.max(2, Math.min(64, parseInt(d.layers || "30", 10)));
    var depth   = Math.max(0, Math.min(12, parseFloat(d.depth || "2.4")));
    var tilt    = Math.max(0, Math.min(12, parseFloat(d.tilt || "7.5")));
    var smooth  = Math.max(0.02, Math.min(0.35, parseFloat(d.smoothing || "0.14")));
    var orbitSp = Math.max(0, Math.min(2, parseFloat(d.orbitSpeed || "0.35")));
    var face = hexToRgb(d.face || "#f8fafc");
    var deep = hexToRgb(d.deep || "#7c3aed");

    root.classList.add("depth-text");
    root.style.perspective = (parseFloat(d.perspective || "900")) + "px";
    root.innerHTML = "";

    var stage = document.createElement("span");
    stage.className = "depth-stage";
    root.appendChild(stage);

    // Back-to-front so the nearest layer paints last.
    for (var i = layers; i >= 1; i--) {
      var progress = i / layers;
      var eased = progress * progress;
      var mix = (1 - eased) * 0.72 + 0.04;   // share of face colour surviving at this depth
      var c = [0, 1, 2].map(function (k) {
        return Math.round(face[k] * mix + deep[k] * (1 - mix));
      });
      var l = document.createElement("span");
      l.className = "depth-layer";
      l.setAttribute("aria-hidden", "true");
      l.style.color = "rgb(" + c.join(",") + ")";
      l.style.transform = "translateZ(" + (-i * depth) + "px)";
      l.textContent = text;
      stage.appendChild(l);
    }

    var faceEl = document.createElement("span");
    faceEl.className = "depth-face";
    faceEl.textContent = text;
    stage.appendChild(faceEl);

    var base = { x: -tilt * 0.32, y: tilt * 0.42 };
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      stage.style.transform = "rotateX(" + base.x + "deg) rotateY(" + base.y + "deg)";
      return;
    }

    var cur = { x: base.x, y: base.y }, tgt = { x: base.x, y: base.y };
    var fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    var active = false, t0 = performance.now();

    if (fine) {
      window.addEventListener("pointermove", function (e) {
        var r = root.getBoundingClientRect();
        if (!r.width || !r.height) return;
        active = true;
        var x = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width / 2)) / (r.width * 0.8)));
        var y = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height / 2)) / (r.height * 0.8)));
        tgt.x = base.x - y * tilt;
        tgt.y = base.y + x * tilt;
      }, { passive: true });
      window.addEventListener("pointerleave", function () {
        active = false; tgt.x = base.x; tgt.y = base.y;
      });
    }

    (function tick(now) {
      requestAnimationFrame(tick);
      if (document.hidden) return;
      if (!active) {
        // Idle orbit so the type never looks frozen — subtler on desktop,
        // where the pointer will take over the moment it moves.
        var o = ((now - t0) / 1000) * orbitSp * Math.PI * 2;
        var amt = fine ? 0.18 : 0.55;
        tgt.x = base.x + Math.sin(o) * tilt * amt;
        tgt.y = base.y + Math.cos(o * 0.85) * tilt * amt;
      }
      cur.x += (tgt.x - cur.x) * smooth;
      cur.y += (tgt.y - cur.y) * smooth;
      stage.style.transform =
        "rotateX(" + cur.x.toFixed(3) + "deg) rotateY(" + cur.y.toFixed(3) + "deg)";
    })(t0);
  }

  function init() {
    var nodes = document.querySelectorAll("[data-depth-text]");
    for (var i = 0; i < nodes.length; i++) build(nodes[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
