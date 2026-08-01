/*
 * Rollcfluence — curved input
 *
 * Vanilla port of the React Bits <CurvedInput />. The original is a React
 * component; this is written as *progressive enhancement* instead: mark up a
 * normal <form> with a real <input> and <button>, and this script replaces
 * the visuals with the curved SVG version. If the script fails to load, or
 * WebGL/SVG is unavailable, or JS is off, the plain form still submits and
 * still works — which matters, because this sits on a page where a real
 * business is trying to sign up.
 *
 * Markup it enhances:
 *   <form data-curved action="/register" method="get">
 *     <input name="name" placeholder="Your business name">
 *     <button>Get my link</button>
 *   </form>
 */
(function () {
  var DEG = 180 / Math.PI;
  function r2(n) { return Math.round(n * 100) / 100; }

  // Maps flat coords (u along the bar, v from the centreline) onto a circular
  // arc with sagitta `bend` px. Positive bend arches up, 0 is flat.
  function buildGeometry(width, bend, thickness, pad) {
    var W = width, T = thickness;
    var s = Math.max(-W * 0.35, Math.min(bend, W * 0.35));
    var a = Math.abs(s), dir = s >= 0 ? 1 : -1;
    var svgH = T + a + pad * 2;
    if (a < 0.75) {
      var midY = pad + T / 2;
      return {
        straight: true, W: W, T: T, svgH: svgH, uPerLen: 1,
        point: function (u, v) { return [u, midY + v]; },
        angleAt: function () { return 0; },
      };
    }
    var R = (W * W * 0.25 + a * a) / (2 * a);
    var cx = W / 2;
    var apexY = pad + T / 2 + (dir > 0 ? 0 : a);
    var cy = apexY + dir * R;
    var phi = Math.asin(Math.min(1, W / (2 * R)));
    return {
      straight: false, W: W, T: T, svgH: svgH, R: R, dir: dir,
      uPerLen: W / (2 * R * phi),
      point: function (u, v) {
        var th = ((u - cx) / cx) * phi;
        var rho = R - dir * v;
        return [cx + rho * Math.sin(th), cy - dir * rho * Math.cos(th)];
      },
      angleAt: function (u) { return dir * ((u - cx) / cx) * phi * DEG; },
    };
  }

  function fmt(g, u, v) { var p = g.point(u, v); return r2(p[0]) + " " + r2(p[1]); }

  function edgeSeg(g, uTo, v, ltr) {
    if (g.straight) return "L " + fmt(g, uTo, v);
    var rho = r2(g.R - g.dir * v);
    var sweep = (ltr === (g.dir > 0)) ? 1 : 0;
    return "A " + rho + " " + rho + " 0 0 " + sweep + " " + fmt(g, uTo, v);
  }

  function bentRect(g, u0, u1, vTop, vBot, radius) {
    var rc = Math.max(0, Math.min(radius, (vBot - vTop) / 2, (u1 - u0) / 2));
    return [
      "M " + fmt(g, u0 + rc, vTop),
      edgeSeg(g, u1 - rc, vTop, true),
      "Q " + fmt(g, u1, vTop) + " " + fmt(g, u1, vTop + rc),
      "L " + fmt(g, u1, vBot - rc),
      "Q " + fmt(g, u1, vBot) + " " + fmt(g, u1 - rc, vBot),
      edgeSeg(g, u0 + rc, vBot, false),
      "Q " + fmt(g, u0, vBot) + " " + fmt(g, u0, vBot - rc),
      "L " + fmt(g, u0, vTop + rc),
      "Q " + fmt(g, u0, vTop) + " " + fmt(g, u0 + rc, vTop),
      "Z",
    ].join(" ");
  }

  function bentLine(g, u0, u1, v) {
    return "M " + fmt(g, u0, v) + " " + edgeSeg(g, u1, v, true);
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function el(name, attrs) {
    var n = document.createElementNS(SVGNS, name);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    return n;
  }

  var uid = 0;

  function enhance(form) {
    var input = form.querySelector("input");
    var button = form.querySelector("button");
    if (!input) return;

    var d = form.dataset;
    var cfg = {
      bend: parseFloat(d.bend || "26"),
      height: parseFloat(d.height || "62"),
      cornerRadius: parseFloat(d.cornerRadius || "18"),
      borderWidth: parseFloat(d.borderWidth || "1.5"),
      fontSize: parseFloat(d.fontSize || "16"),
      bg: d.bg || "rgba(255,255,255,0.07)",
      textColor: d.textColor || "#ffffff",
      placeholderColor: d.placeholderColor || "rgba(255,255,255,0.5)",
      borderColor: d.borderColor || "rgba(255,255,255,0.22)",
      buttonColor: d.buttonColor || "#a855f7",
      buttonTextColor: d.buttonTextColor || "#ffffff",
    };
    var buttonText = (button && button.textContent.trim()) || "Go";
    var placeholder = input.getAttribute("placeholder") || "";

    var id = "ci" + (++uid);
    var pathId = id + "-t", btnPathId = id + "-b", clipId = id + "-c";

    var host = document.createElement("div");
    host.className = "ci-host";
    host.style.cssText = "position:relative;width:100%;cursor:text;";
    form.insertBefore(host, form.firstChild);

    // Keep the real input in the DOM (accessible, submits normally) but
    // visually replaced by the SVG we draw on top of it.
    input.style.cssText =
      "position:absolute;inset:0;width:100%;height:100%;opacity:0;border:0;padding:0;margin:0;" +
      "background:transparent;color:transparent;caret-color:transparent;font-size:16px;outline:none;";
    host.appendChild(input);
    if (button) button.style.display = "none";

    var svg = el("svg", { class: "ci-svg" });
    svg.style.cssText = "display:block;overflow:visible;width:100%;height:auto;user-select:none;";
    host.insertBefore(svg, input);

    var textEl, phEl, caretG, measureEl;
    var focused = false, caretIndex = 0, geom = null, layout = null;

    function measureButtonWidth() {
      if (!measureEl) return cfg.fontSize * 5;
      try { return measureEl.getComputedTextLength(); } catch (e) { return cfg.fontSize * 5; }
    }

    function render() {
      var W = Math.round(host.clientWidth);
      if (W < 40) return;
      var pad = Math.ceil(cfg.borderWidth / 2) + 6;
      geom = buildGeometry(W, cfg.bend, cfg.height, pad);

      while (svg.firstChild) svg.removeChild(svg.firstChild);
      svg.setAttribute("width", W);
      svg.setAttribute("height", r2(geom.svgH));
      svg.setAttribute("viewBox", "0 0 " + W + " " + r2(geom.svgH));

      var defs = el("defs");
      svg.appendChild(defs);

      // hidden measuring text for the button label
      measureEl = el("text", { x: -9999, y: -9999, visibility: "hidden" });
      measureEl.style.fontSize = cfg.fontSize + "px";
      measureEl.style.fontWeight = "600";
      measureEl.textContent = buttonText;
      svg.appendChild(measureEl);

      var T = cfg.height;
      var btnInset = Math.max(5, cfg.borderWidth + 4);
      var btnW = Math.max(measureButtonWidth() + cfg.fontSize * 2.6, T * 1.3);
      var btnU1 = W - btnInset, btnU0 = btnU1 - btnW;
      var textStartU = 24;
      var textEndU = Math.max(textStartU + 20, btnU0 - 14);
      layout = { btnU0: btnU0, btnU1: btnU1, btnInset: btnInset, textStartU: textStartU, textEndU: textEndU };

      var vBase = cfg.fontSize * 0.34;
      svg.appendChild(el("path", {
        d: bentRect(geom, 0, W, -T / 2, T / 2, cfg.cornerRadius),
        fill: cfg.bg, stroke: cfg.borderColor, "stroke-width": cfg.borderWidth,
      }));

      var clip = el("clipPath", { id: clipId });
      clip.appendChild(el("path", { d: bentRect(geom, textStartU - 6, textEndU + 8, -T / 2, T / 2, 0) }));
      defs.appendChild(clip);

      defs.appendChild(el("path", { id: pathId, d: bentLine(geom, textStartU, W, vBase), fill: "none" }));

      var g = el("g", { "clip-path": "url(#" + clipId + ")" });
      phEl = el("text", { fill: cfg.placeholderColor });
      phEl.style.fontSize = cfg.fontSize + "px";
      var phPath = el("textPath"); phPath.setAttribute("href", "#" + pathId);
      phPath.textContent = placeholder; phEl.appendChild(phPath);
      g.appendChild(phEl);

      textEl = el("text", { fill: cfg.textColor });
      textEl.style.fontSize = cfg.fontSize + "px";
      textEl.style.fontWeight = "500";
      var tPath = el("textPath"); tPath.setAttribute("href", "#" + pathId);
      textEl.appendChild(tPath); g.appendChild(textEl);

      caretG = el("g"); g.appendChild(caretG);
      svg.appendChild(g);

      // curved submit button
      var btnH = T - btnInset * 2;
      var bg = el("g", { class: "ci-btn", role: "button", tabindex: "0" });
      bg.style.cursor = "pointer";
      bg.appendChild(el("path", {
        d: bentRect(geom, btnU0, btnU1, -T / 2 + btnInset, T / 2 - btnInset,
                    Math.min(cfg.cornerRadius * 0.72, btnH / 2)),
        fill: cfg.buttonColor,
      }));
      defs.appendChild(el("path", { id: btnPathId, d: bentLine(geom, btnU0, btnU1, vBase), fill: "none" }));
      var bt = el("text", { fill: cfg.buttonTextColor, "text-anchor": "middle" });
      bt.style.fontSize = cfg.fontSize + "px";
      bt.style.fontWeight = "600";
      bt.style.pointerEvents = "none";
      var btp = el("textPath"); btp.setAttribute("href", "#" + btnPathId);
      btp.setAttribute("startOffset", "50%"); btp.textContent = buttonText;
      bt.appendChild(btp); bg.appendChild(bt);
      bg.addEventListener("click", function (e) { e.stopPropagation(); submit(); });
      bg.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); submit(); }
      });
      svg.appendChild(bg);

      paint();
    }

    function paint() {
      if (!geom || !textEl) return;
      var val = input.value;
      textEl.firstChild.textContent = val;
      phEl.style.display = val ? "none" : "";

      while (caretG.firstChild) caretG.removeChild(caretG.firstChild);
      if (!focused) return;
      var caretLen = 0;
      try {
        var idx = Math.min(caretIndex, val.length);
        caretLen = idx > 0 ? textEl.getSubStringLength(0, idx) : 0;
      } catch (e) { caretLen = 0; }
      var u = layout.textStartU + caretLen * geom.uPerLen;
      if (u > layout.textEndU) u = layout.textEndU;
      var p = geom.point(u, 0);
      var ang = geom.angleAt(u);
      var h = Math.min(cfg.height * 0.58, cfg.fontSize * 1.45);
      var gg = el("g", { transform: "translate(" + r2(p[0]) + " " + r2(p[1]) + ") rotate(" + r2(ang) + ")" });
      var line = el("line", { y1: -h / 2, y2: h / 2, stroke: cfg.textColor, "stroke-width": 1.5, "stroke-linecap": "round" });
      var anim = el("animate", { attributeName: "opacity", values: "1;0", dur: "1.06s", calcMode: "discrete", repeatCount: "indefinite" });
      line.appendChild(anim); gg.appendChild(line); caretG.appendChild(gg);
    }

    function submit() {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }

    host.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".ci-btn")) return;
      e.preventDefault(); input.focus();
    });
    input.addEventListener("input", function () { caretIndex = input.selectionStart || input.value.length; paint(); });
    input.addEventListener("select", function () { caretIndex = input.selectionStart || 0; paint(); });
    input.addEventListener("keyup", function () { caretIndex = input.selectionStart || 0; paint(); });
    input.addEventListener("focus", function () { focused = true; paint(); });
    input.addEventListener("blur", function () { focused = false; paint(); });

    render();
    if (window.ResizeObserver) new ResizeObserver(render).observe(host);
    else window.addEventListener("resize", render);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
  }

  function init() {
    var forms = document.querySelectorAll("[data-curved]");
    for (var i = 0; i < forms.length; i++) enhance(forms[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
