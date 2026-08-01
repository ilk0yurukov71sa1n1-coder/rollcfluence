/*
 * Rollcfluence dithered wave background — vanilla Three.js port of the
 * "Dither" React Bits component (wave shader + Bayer-matrix dithering),
 * combined into a single fragment-shader pass so it runs with just
 * three.js from a CDN — no React, no @react-three/fiber, no
 * @react-three/postprocessing, no build step.
 *
 * Usage: <div id="dither-canvas-wrap" data-color="0.24,0.39,0.87" ...></div>
 * then include this script after three.js has loaded.
 */
(function () {
  function init() {
    var container = document.getElementById("dither-canvas-wrap");
    if (!container || typeof THREE === "undefined") return;

    // ---- config (mirrors the React Bits <Dither /> props) ----
    var cfg = {
      waveSpeed: parseFloat(container.dataset.waveSpeed || "0.05"),
      waveFrequency: parseFloat(container.dataset.waveFrequency || "3"),
      waveAmplitude: parseFloat(container.dataset.waveAmplitude || "0.3"),
      waveColor: (container.dataset.color || "0.24,0.39,0.87")
        .split(",")
        .map(Number),
      colorNum: parseFloat(container.dataset.colorNum || "4"),
      pixelSize: parseFloat(container.dataset.pixelSize || "2"),
      disableAnimation: container.dataset.disableAnimation === "true",
      enableMouseInteraction: container.dataset.mouseInteraction !== "false",
      mouseRadius: parseFloat(container.dataset.mouseRadius || "0.3"),
    };

    var canvas = document.createElement("canvas");
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    container.appendChild(canvas);

    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({
        canvas: canvas,
        antialias: true,
        alpha: false,
        preserveDrawingBuffer: false,
      });
    } catch (e) {
      return; // no WebGL — CSS gradient fallback on the container stays visible
    }
    renderer.setPixelRatio(1);

    var scene = new THREE.Scene();
    var camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

    var uniforms = {
      time: { value: 0 },
      resolution: { value: new THREE.Vector2(1, 1) },
      waveSpeed: { value: cfg.waveSpeed },
      waveFrequency: { value: cfg.waveFrequency },
      waveAmplitude: { value: cfg.waveAmplitude },
      waveColor: {
        value: new THREE.Vector3(cfg.waveColor[0], cfg.waveColor[1], cfg.waveColor[2]),
      },
      mousePos: { value: new THREE.Vector2(0, 0) },
      enableMouseInteraction: { value: cfg.enableMouseInteraction ? 1 : 0 },
      mouseRadius: { value: cfg.mouseRadius },
      colorNum: { value: cfg.colorNum },
      pixelSize: { value: cfg.pixelSize },
    };

    var vertexShader = [
      "void main() {",
      "  gl_Position = vec4(position.xy, 0.0, 1.0);",
      "}",
    ].join("\n");

    // Wave pattern (classic-noise fbm) + Bayer-matrix dithering, combined
    // into one GLSL ES 1.00 (WebGL1-safe) pass. The Bayer matrix is looked
    // up via a branch table instead of an array-literal constructor, since
    // `float[64](...)` initializer syntax is GLSL ES 3.00 only and would
    // fail to compile under a WebGL1 context.
    var fragmentShader = [
      "precision highp float;",
      "uniform vec2 resolution;",
      "uniform float time;",
      "uniform float waveSpeed;",
      "uniform float waveFrequency;",
      "uniform float waveAmplitude;",
      "uniform vec3 waveColor;",
      "uniform vec2 mousePos;",
      "uniform int enableMouseInteraction;",
      "uniform float mouseRadius;",
      "uniform float colorNum;",
      "uniform float pixelSize;",

      "vec4 mod289(vec4 x) { return x - floor(x * (1.0/289.0)) * 289.0; }",
      "vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }",
      "vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }",
      "vec2 fade(vec2 t) { return t*t*t*(t*(t*6.0-15.0)+10.0); }",

      "float cnoise(vec2 P) {",
      "  vec4 Pi = floor(P.xyxy) + vec4(0.0,0.0,1.0,1.0);",
      "  vec4 Pf = fract(P.xyxy) - vec4(0.0,0.0,1.0,1.0);",
      "  Pi = mod289(Pi);",
      "  vec4 ix = Pi.xzxz; vec4 iy = Pi.yyww;",
      "  vec4 fx = Pf.xzxz; vec4 fy = Pf.yyww;",
      "  vec4 i = permute(permute(ix) + iy);",
      "  vec4 gx = fract(i * (1.0/41.0)) * 2.0 - 1.0;",
      "  vec4 gy = abs(gx) - 0.5;",
      "  vec4 tx = floor(gx + 0.5);",
      "  gx = gx - tx;",
      "  vec2 g00 = vec2(gx.x, gy.x); vec2 g10 = vec2(gx.y, gy.y);",
      "  vec2 g01 = vec2(gx.z, gy.z); vec2 g11 = vec2(gx.w, gy.w);",
      "  vec4 norm = taylorInvSqrt(vec4(dot(g00,g00), dot(g01,g01), dot(g10,g10), dot(g11,g11)));",
      "  g00 *= norm.x; g01 *= norm.y; g10 *= norm.z; g11 *= norm.w;",
      "  float n00 = dot(g00, vec2(fx.x, fy.x));",
      "  float n10 = dot(g10, vec2(fx.y, fy.y));",
      "  float n01 = dot(g01, vec2(fx.z, fy.z));",
      "  float n11 = dot(g11, vec2(fx.w, fy.w));",
      "  vec2 fade_xy = fade(Pf.xy);",
      "  vec2 n_x = mix(vec2(n00, n01), vec2(n10, n11), fade_xy.x);",
      "  return 2.3 * mix(n_x.x, n_x.y, fade_xy.y);",
      "}",

      "float fbm(vec2 p) {",
      "  float value = 0.0; float amp = 1.0; float freq = waveFrequency;",
      "  for (int i = 0; i < 4; i++) {",
      "    value += amp * abs(cnoise(p));",
      "    p *= freq;",
      "    amp *= waveAmplitude;",
      "  }",
      "  return value;",
      "}",

      "float pattern(vec2 p) {",
      "  vec2 p2 = p - time * waveSpeed;",
      "  return fbm(p + fbm(p2));",
      "}",

      // Bayer 8x8 threshold lookup via branch table (WebGL1-safe).
      "float bayerValue(int idx) {",
      "  if (idx < 32) {",
      "    if (idx < 16) {",
      "      if (idx < 8) {",
      "        if (idx==0) return 0.0/64.0; if (idx==1) return 48.0/64.0; if (idx==2) return 12.0/64.0; if (idx==3) return 60.0/64.0;",
      "        if (idx==4) return 3.0/64.0; if (idx==5) return 51.0/64.0; if (idx==6) return 15.0/64.0; return 63.0/64.0;",
      "      } else {",
      "        if (idx==8) return 32.0/64.0; if (idx==9) return 16.0/64.0; if (idx==10) return 44.0/64.0; if (idx==11) return 28.0/64.0;",
      "        if (idx==12) return 35.0/64.0; if (idx==13) return 19.0/64.0; if (idx==14) return 47.0/64.0; return 31.0/64.0;",
      "      }",
      "    } else {",
      "      if (idx < 24) {",
      "        if (idx==16) return 8.0/64.0; if (idx==17) return 56.0/64.0; if (idx==18) return 4.0/64.0; if (idx==19) return 52.0/64.0;",
      "        if (idx==20) return 11.0/64.0; if (idx==21) return 59.0/64.0; if (idx==22) return 7.0/64.0; return 55.0/64.0;",
      "      } else {",
      "        if (idx==24) return 40.0/64.0; if (idx==25) return 24.0/64.0; if (idx==26) return 36.0/64.0; if (idx==27) return 20.0/64.0;",
      "        if (idx==28) return 43.0/64.0; if (idx==29) return 27.0/64.0; if (idx==30) return 39.0/64.0; return 23.0/64.0;",
      "      }",
      "    }",
      "  } else {",
      "    if (idx < 48) {",
      "      if (idx < 40) {",
      "        if (idx==32) return 2.0/64.0; if (idx==33) return 50.0/64.0; if (idx==34) return 14.0/64.0; if (idx==35) return 62.0/64.0;",
      "        if (idx==36) return 1.0/64.0; if (idx==37) return 49.0/64.0; if (idx==38) return 13.0/64.0; return 61.0/64.0;",
      "      } else {",
      "        if (idx==40) return 34.0/64.0; if (idx==41) return 18.0/64.0; if (idx==42) return 46.0/64.0; if (idx==43) return 30.0/64.0;",
      "        if (idx==44) return 33.0/64.0; if (idx==45) return 17.0/64.0; if (idx==46) return 45.0/64.0; return 29.0/64.0;",
      "      }",
      "    } else {",
      "      if (idx < 56) {",
      "        if (idx==48) return 10.0/64.0; if (idx==49) return 58.0/64.0; if (idx==50) return 6.0/64.0; if (idx==51) return 54.0/64.0;",
      "        if (idx==52) return 9.0/64.0; if (idx==53) return 57.0/64.0; if (idx==54) return 5.0/64.0; return 53.0/64.0;",
      "      } else {",
      "        if (idx==56) return 42.0/64.0; if (idx==57) return 26.0/64.0; if (idx==58) return 38.0/64.0; if (idx==59) return 22.0/64.0;",
      "        if (idx==60) return 41.0/64.0; if (idx==61) return 25.0/64.0; if (idx==62) return 37.0/64.0; return 21.0/64.0;",
      "      }",
      "    }",
      "  }",
      "}",

      "vec3 ditherColor(vec2 fragCoord, vec3 color) {",
      "  vec2 scaledCoord = floor(fragCoord / pixelSize);",
      "  int x = int(mod(scaledCoord.x, 8.0));",
      "  int y = int(mod(scaledCoord.y, 8.0));",
      "  float threshold = bayerValue(y * 8 + x) - 0.25;",
      "  float ditherStep = 1.0 / (colorNum - 1.0);",
      "  color += threshold * ditherStep;",
      "  color = clamp(color - 0.2, 0.0, 1.0);",
      "  return floor(color * (colorNum - 1.0) + 0.5) / (colorNum - 1.0);",
      "}",

      "void main() {",
      "  vec2 fragUV = gl_FragCoord.xy / resolution.xy;",
      "  vec2 normPixel = pixelSize / resolution;",
      "  vec2 snappedUV = normPixel * floor(fragUV / max(normPixel, vec2(0.0001)));",
      "  vec2 uv = snappedUV - 0.5;",
      "  uv.x *= resolution.x / resolution.y;",

      "  float f = pattern(uv);",

      "  if (enableMouseInteraction == 1) {",
      "    vec2 mouseNDC = (mousePos / resolution - 0.5) * vec2(1.0, -1.0);",
      "    mouseNDC.x *= resolution.x / resolution.y;",
      "    float dist = length(uv - mouseNDC);",
      "    float effect = 1.0 - smoothstep(0.0, mouseRadius, dist);",
      "    f -= 0.5 * effect;",
      "  }",

      "  vec3 col = mix(vec3(0.0), waveColor, f);",
      "  col = ditherColor(gl_FragCoord.xy, col);",
      "  gl_FragColor = vec4(col, 1.0);",
      "}",
    ].join("\n");

    var material = new THREE.ShaderMaterial({
      uniforms: uniforms,
      vertexShader: vertexShader,
      fragmentShader: fragmentShader,
    });
    var quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material);
    scene.add(quad);

    function resize() {
      var w = Math.max(1, container.clientWidth);
      var h = Math.max(1, container.clientHeight);
      renderer.setSize(w, h, true);
      uniforms.resolution.value.set(w, h);
    }
    window.addEventListener("resize", resize);
    resize();

    container.style.cursor = cfg.enableMouseInteraction ? "crosshair" : "default";
    container.addEventListener("pointermove", function (e) {
      if (!cfg.enableMouseInteraction) return;
      var rect = container.getBoundingClientRect();
      uniforms.mousePos.value.set(e.clientX - rect.left, e.clientY - rect.top);
    });

    var clock = new THREE.Clock();
    function animate() {
      requestAnimationFrame(animate);
      if (document.hidden) return; // pause off-screen tabs — battery/CPU friendly
      if (!cfg.disableAnimation) {
        uniforms.time.value = clock.getElapsedTime();
      }
      renderer.render(scene, camera);
    }
    animate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
