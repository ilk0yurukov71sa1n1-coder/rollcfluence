/*
 * Rollcfluence — Ferrofluid background
 *
 * Vanilla-WebGL port of the React Bits <Ferrofluid /> component. The original
 * ships as React + ogl; this runs the identical fragment shader with ~60 lines
 * of raw WebGL instead, so it drops into a plain HTML page with no React, no
 * ogl, no npm and no build step.
 *
 * The shader is GLSL ES 1.00 (varying / gl_FragColor), so it runs on WebGL1 —
 * which means it works on essentially every phone and browser in use.
 *
 * Usage:
 *   <div class="ferro" data-colors="#7aa2ff,#c77dff,#ff5fa8" data-speed="0.5"></div>
 * Every matching container gets its own independent instance.
 */
(function () {
  var VERT = [
    "attribute vec2 position;",
    "varying vec2 vUv;",
    "void main() {",
    "  vUv = position * 0.5 + 0.5;",
    "  gl_Position = vec4(position, 0.0, 1.0);",
    "}",
  ].join("\n");

  var FRAG = [
    "precision highp float;",
    "uniform vec3  iResolution;",
    "uniform vec2  iMouse;",
    "uniform float iTime;",
    "uniform vec3  uColor0; uniform vec3 uColor1; uniform vec3 uColor2; uniform vec3 uColor3;",
    "uniform vec3  uColor4; uniform vec3 uColor5; uniform vec3 uColor6; uniform vec3 uColor7;",
    "uniform int   uColorCount;",
    "uniform vec2  uFlow;",
    "uniform float uSpeed;",
    "uniform float uScale;",
    "uniform float uTurbulence;",
    "uniform float uFluidity;",
    "uniform float uRimWidth;",
    "uniform float uSharpness;",
    "uniform float uShimmer;",
    "uniform float uGlow;",
    "uniform float uOpacity;",
    "uniform float uMouseEnabled;",
    "uniform float uMouseStrength;",
    "uniform float uMouseRadius;",
    "varying vec2 vUv;",
    "#define PI 3.14159265",

    "vec3 palette(float h) {",
    "  int count = uColorCount;",
    "  if (count < 1) count = 1;",
    "  int idx = int(floor(clamp(h, 0.0, 0.999999) * float(count)));",
    "  if (idx <= 0) return uColor0;",
    "  if (idx == 1) return uColor1;",
    "  if (idx == 2) return uColor2;",
    "  if (idx == 3) return uColor3;",
    "  if (idx == 4) return uColor4;",
    "  if (idx == 5) return uColor5;",
    "  if (idx == 6) return uColor6;",
    "  return uColor7;",
    "}",

    "float hash(vec3 p3) {",
    "  p3 = fract(p3 * 0.1031);",
    "  p3 += dot(p3, p3.zyx + 33.33);",
    "  return fract((p3.x + p3.y) * p3.z);",
    "}",
    "float smin(float a, float b, float k) {",
    "  float r = exp2(-a / k) + exp2(-b / k);",
    "  return -k * log2(r);",
    "}",
    "float sinlerp(float a, float b, float w) {",
    "  return mix(a, b, (sin(w * PI - PI / 2.0) + 1.0) / 2.0);",
    "}",
    "float vn(vec2 p, float s, float seed) {",
    "  vec2 cellp = floor(p / s);",
    "  vec2 relp = mod(p, s);",
    "  float g1 = hash(vec3(cellp, seed));",
    "  float g2 = hash(vec3(cellp.x + 1.0, cellp.y, seed));",
    "  float g3 = hash(vec3(cellp.x + 1.0, cellp.y + 1.0, seed));",
    "  float g4 = hash(vec3(cellp.x, cellp.y + 1.0, seed));",
    "  float bx = sinlerp(g1, g2, relp.x / s);",
    "  float tx = sinlerp(g4, g3, relp.x / s);",
    "  return sinlerp(bx, tx, relp.y / s);",
    "}",
    "float dbn(vec2 p, float s, float seed) {",
    "  float o = s / 2.0;",
    "  float n0 = vn(p, s, seed);",
    "  float n1 = vn(p + vec2(o, o), s, seed + 0.1);",
    "  float n2 = vn(p + vec2(-o, o), s, seed + 0.2);",
    "  float n3 = vn(p + vec2(o, -o), s, seed + 0.3);",
    "  float n4 = vn(p + vec2(-o, -o), s, seed + 0.4);",
    "  return (2.0 * n0 + 1.5 * n1 + 1.25 * n2 + 1.125 * n3 + n4) / 7.0;",
    "}",

    "void main() {",
    "  vec2 fragCoord = vUv * iResolution.xy;",
    "  float ref = 700.0 / max(uScale, 0.05);",
    "  vec2 p = fragCoord / iResolution.y * ref;",
    "  float spd = 200.0 * uSpeed;",
    "  float t = iTime;",
    "  vec2 dir = uFlow;",
    "  vec2 perp = vec2(-dir.y, dir.x);",
    "  float distort1 = vn(p + perp * (t * spd), 60.0, 10.0) * 50.0 * uTurbulence;",
    "  float distort2 = vn(p - perp * (t * spd), 120.0, 15.0) * 100.0 * uTurbulence;",
    "  float peaks = dbn(p + distort1 + dir * (t * spd * 0.5), 40.0, 1.0);",
    "  float peaks2 = dbn(p + distort2 - dir * (t * spd * 0.5), 40.0, 0.0);",
    "  float mapeaks = smin(peaks, peaks2, max(uFluidity, 0.001));",
    "  float mGlow = 0.0;",
    "  if (uMouseEnabled > 0.5) {",
    "    vec2 mp = iMouse / iResolution.y * ref;",
    "    float md = length(p - mp) / ref;",
    "    float rr = max(uMouseRadius, 0.02);",
    "    mGlow = exp(-md * md / (rr * rr)) * uMouseStrength;",
    "  }",
    "  float band = (uRimWidth - abs((mapeaks - 0.4) * 2.0)) * 5.0;",
    "  float ltn = clamp(band - vn(p + dir * (t * spd * 0.5), 60.0, 12.0) * uShimmer, 0.0, 1.0);",
    "  ltn = pow(ltn, uSharpness) * uGlow;",
    "  ltn *= clamp(1.0 - mGlow, 0.0, 1.0);",
    "  float h = clamp(0.5 + (peaks - peaks2) * 0.8, 0.0, 1.0);",
    "  vec3 col = palette(h);",
    "  vec3 outc = col * ltn;",
    "  float a = clamp(max(outc.r, max(outc.g, outc.b)), 0.0, 1.0);",
    "  gl_FragColor = vec4(outc, a * uOpacity);",
    "}",
  ].join("\n");

  function hexToRGB(hex) {
    var c = String(hex).replace("#", "").trim();
    if (c.length === 3) c = c[0] + c[0] + c[1] + c[1] + c[2] + c[2];
    c = (c + "000000").slice(0, 6);
    return [
      parseInt(c.slice(0, 2), 16) / 255,
      parseInt(c.slice(2, 4), 16) / 255,
      parseInt(c.slice(4, 6), 16) / 255,
    ];
  }

  function flowVec(d) {
    if (d === "up") return [0, 1];
    if (d === "left") return [-1, 0];
    if (d === "right") return [1, 0];
    return [0, -1];
  }

  function compile(gl, type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn("[ferrofluid] shader error:", gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function initOne(container) {
    var d = container.dataset;
    var cfg = {
      colors: (d.colors || "#7aa2ff,#c77dff,#ff5fa8").split(",").map(function (s) { return s.trim(); }),
      speed: parseFloat(d.speed || "0.5"),
      scale: parseFloat(d.scale || "1.6"),
      turbulence: parseFloat(d.turbulence || "1"),
      fluidity: parseFloat(d.fluidity || "0.1"),
      rimWidth: parseFloat(d.rimWidth || "0.2"),
      sharpness: parseFloat(d.sharpness || "2.5"),
      shimmer: parseFloat(d.shimmer || "1.5"),
      glow: parseFloat(d.glow || "2"),
      flowDirection: d.flowDirection || "down",
      opacity: parseFloat(d.opacity || "1"),
      mouseInteraction: d.mouseInteraction !== "false",
      mouseStrength: parseFloat(d.mouseStrength || "1"),
      mouseRadius: parseFloat(d.mouseRadius || "0.35"),
      mouseDampening: parseFloat(d.mouseDampening || "0.15"),
    };

    var canvas = document.createElement("canvas");
    canvas.style.cssText = "display:block;width:100%;height:100%;";
    container.appendChild(canvas);

    var gl = canvas.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: false })
          || canvas.getContext("experimental-webgl", { alpha: true });
    if (!gl) return; // no WebGL — container's CSS gradient stays as the fallback

    var vs = compile(gl, gl.VERTEX_SHADER, VERT);
    var fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn("[ferrofluid] link error:", gl.getProgramInfoLog(prog));
      return;
    }
    gl.useProgram(prog);

    // Full-screen triangle — cheaper than a quad, no index buffer needed.
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, "position");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    function U(n) { return gl.getUniformLocation(prog, n); }
    var u = {
      iResolution: U("iResolution"), iMouse: U("iMouse"), iTime: U("iTime"),
      uColorCount: U("uColorCount"), uFlow: U("uFlow"), uSpeed: U("uSpeed"),
      uScale: U("uScale"), uTurbulence: U("uTurbulence"), uFluidity: U("uFluidity"),
      uRimWidth: U("uRimWidth"), uSharpness: U("uSharpness"), uShimmer: U("uShimmer"),
      uGlow: U("uGlow"), uOpacity: U("uOpacity"), uMouseEnabled: U("uMouseEnabled"),
      uMouseStrength: U("uMouseStrength"), uMouseRadius: U("uMouseRadius"),
    };

    var pal = cfg.colors.slice(0, 8);
    for (var i = 0; i < 8; i++) {
      var rgb = hexToRGB(pal[Math.min(i, pal.length - 1)]);
      gl.uniform3f(U("uColor" + i), rgb[0], rgb[1], rgb[2]);
    }
    gl.uniform1i(u.uColorCount, pal.length);
    var fv = flowVec(cfg.flowDirection);
    gl.uniform2f(u.uFlow, fv[0], fv[1]);
    gl.uniform1f(u.uSpeed, cfg.speed);
    gl.uniform1f(u.uScale, cfg.scale);
    gl.uniform1f(u.uTurbulence, cfg.turbulence);
    gl.uniform1f(u.uFluidity, cfg.fluidity);
    gl.uniform1f(u.uRimWidth, cfg.rimWidth);
    gl.uniform1f(u.uSharpness, cfg.sharpness);
    gl.uniform1f(u.uShimmer, cfg.shimmer);
    gl.uniform1f(u.uGlow, cfg.glow);
    gl.uniform1f(u.uOpacity, cfg.opacity);
    gl.uniform1f(u.uMouseEnabled, cfg.mouseInteraction ? 1 : 0);
    gl.uniform1f(u.uMouseStrength, cfg.mouseStrength);
    gl.uniform1f(u.uMouseRadius, cfg.mouseRadius);

    // Cap DPR at 1.5: this shader is noise-heavy and full-res on a 3x phone
    // screen burns battery for detail nobody can see at background opacity.
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    function resize() {
      var w = Math.max(1, Math.floor(container.clientWidth * dpr));
      var h = Math.max(1, Math.floor(container.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w; canvas.height = h;
        gl.viewport(0, 0, w, h);
        gl.uniform3f(u.iResolution, w, h, 1);
      }
    }
    resize();
    if (window.ResizeObserver) new ResizeObserver(resize).observe(container);
    else window.addEventListener("resize", resize);

    var mTarget = [0, 0], mCur = [0, 0];
    if (cfg.mouseInteraction) {
      container.addEventListener("pointermove", function (e) {
        var r = canvas.getBoundingClientRect();
        mTarget = [(e.clientX - r.left) * dpr, (r.height - (e.clientY - r.top)) * dpr];
        if (cfg.mouseDampening <= 0) mCur = mTarget.slice();
      });
    }

    var start = performance.now(), last = start;
    function frame(now) {
      requestAnimationFrame(frame);
      if (document.hidden) { last = now; return; }   // don't burn CPU in a background tab
      var dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      if (cfg.mouseDampening > 0) {
        var f = 1 - Math.exp(-dt / Math.max(1e-4, cfg.mouseDampening));
        mCur[0] += (mTarget[0] - mCur[0]) * f;
        mCur[1] += (mTarget[1] - mCur[1]) * f;
      }
      gl.uniform2f(u.iMouse, mCur[0], mCur[1]);
      gl.uniform1f(u.iTime, (now - start) * 0.001);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    requestAnimationFrame(frame);
  }

  function init() {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var nodes = document.querySelectorAll("[data-ferrofluid]");
    for (var i = 0; i < nodes.length; i++) initOne(nodes[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
