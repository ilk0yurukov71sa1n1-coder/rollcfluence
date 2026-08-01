/*
 * Rollcfluence — specular edge highlight
 *
 * Vanilla equivalent of the React Bits <SpecularButton />. The original draws
 * its rim light with a WebGL shader via ogl; that's a whole GL context per
 * button, which is heavy when a page has several. This reproduces the same
 * effect — a bright streak on the border that points toward the cursor and
 * fades in as the cursor approaches — using a CSS conic-gradient border and
 * two custom properties updated on pointermove. Cheap, and visually very
 * close at the sizes these controls actually render at.
 *
 * Apply to anything with class "spec": buttons, inputs, cards.
 */
(function () {
  var PROXIMITY = 260;   // px — how close the cursor must get before the rim lights up

  function init() {
    var nodes = document.querySelectorAll(".spec");
    if (!nodes.length) return;

    function onMove(e) {
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var r = el.getBoundingClientRect();
        if (!r.width) continue;
        var cx = r.left + r.width / 2;
        var cy = r.top + r.height / 2;

        // Distance from the cursor to the element's *edge*, not its centre —
        // so a wide button lights up when you approach any part of it.
        var dx = Math.max(r.left - e.clientX, 0, e.clientX - r.right);
        var dy = Math.max(r.top - e.clientY, 0, e.clientY - r.bottom);
        var dist = Math.hypot(dx, dy);

        var t = Math.max(0, 1 - dist / PROXIMITY);
        t = t * t * (3 - 2 * t);   // smoothstep, so the fade-in feels natural rather than linear

        var angle = Math.atan2(e.clientY - cy, e.clientX - cx) * 180 / Math.PI;
        el.style.setProperty("--spec-angle", (angle + 90).toFixed(1) + "deg");
        el.style.setProperty("--spec-on", t.toFixed(3));
      }
    }

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerleave", function () {
      for (var i = 0; i < nodes.length; i++) nodes[i].style.setProperty("--spec-on", "0");
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
