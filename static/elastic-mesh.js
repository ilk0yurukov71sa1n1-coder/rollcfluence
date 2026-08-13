/*
 * Rollcfluence — ElasticMesh
 *
 * Vanilla-WebGL port of the React Bits <ElasticMesh />. The original needs
 * React + ogl; this runs the same two shaders and the same CPU spring
 * simulation against raw WebGL buffers, so it drops into a plain page.
 *
 * How it works: the sheet is a grid of nodes, each spring-linked to its four
 * neighbours and to its own rest position. The pointer pulls nearby nodes
 * toward it (and pushes them forward in Z); the springs drag everything back
 * to flat. Normals are recomputed every frame from the deformed positions,
 * which is what makes the lighting read as a real stretching surface.
 *
 * Usage:
 *   <div data-elastic-mesh data-color1="#4f7cff" data-color2="#ff4fa0"></div>
 */
(function () {
  var DIST = 4.6, FIT = 0.82;

  var VERT = [
"precision highp float;",
"attribute vec2 aGrid; attribute vec2 uv; attribute vec3 aOffset; attribute vec3 aNormal;",
"uniform float uAspect; uniform float uTilt; uniform float uDist; uniform float uFit;",
"varying vec2 vUv; varying vec3 vNormal; varying float vDepth;",
"void main(){ vUv=uv;",
"  vec2 base=vec2((aGrid.x*2.0-1.0)*uAspect, 1.0-aGrid.y*2.0);",
"  vec3 p=vec3(base+aOffset.xy, aOffset.z);",
"  float ct=cos(uTilt), st=sin(uTilt);",
"  float ry=p.y*ct-p.z*st; float rz=p.y*st+p.z*ct; p.y=ry; p.z=rz;",
"  float persp=uDist/(uDist-p.z);",
"  vec2 clip=vec2(p.x/uAspect, p.y)*persp*uFit;",
"  vNormal=aNormal; vDepth=aOffset.z;",
"  gl_Position=vec4(clip,0.0,1.0); }"].join("\n");

  var FRAG = [
"precision highp float;",
"varying vec2 vUv; varying vec3 vNormal; varying float vDepth;",
"uniform vec3 uColor1; uniform vec3 uColor2; uniform vec3 uHighlight;",
"uniform float uShading; uniform vec2 uRes; uniform float uRadius;",
"uniform float uGrid; uniform float uGridDensity; uniform float uGridOpacity; uniform vec3 uGridColor;",
"void main(){",
"  vec3 base=mix(uColor1,uColor2,clamp(vUv.y,0.0,1.0));",
"  vec3 N=normalize(vNormal); vec3 L=normalize(vec3(-0.35,0.55,0.78));",
"  vec3 V=vec3(0.0,0.0,1.0); vec3 H=normalize(L+V);",
"  float diff=clamp(dot(N,L),0.0,1.0);",
"  float specRaw=pow(clamp(dot(N,H),0.0,1.0),26.0);",
"  float specFlat=pow(clamp(H.z,0.0,1.0),26.0);",
"  float spec=clamp((specRaw-specFlat)/(1.0-specFlat),0.0,1.0);",
"  float ao=clamp(1.0+vDepth*0.45,0.65,1.25);",
"  vec3 lit=base*(1.0-uShading*0.28);",
"  lit+=base*diff*uShading*0.55; lit*=ao;",
"  lit+=uHighlight*spec*uShading*0.25;",
"  if(uGrid>0.5){ vec2 g=vUv*uGridDensity;",
"    vec2 w=uGridDensity/max(uRes,vec2(1.0));",
"    vec2 d=abs(fract(g-0.5)-0.5)/max(w*1.5,vec2(1e-4));",
"    float line=1.0-clamp(min(d.x,d.y),0.0,1.0);",
"    lit=mix(lit,uGridColor,line*uGridOpacity*(0.45+diff*0.55)); }",
"  vec2 p=(vUv-0.5)*uRes; vec2 hr=uRes*0.5;",
"  float r=min(uRadius,min(hr.x,hr.y)); vec2 q=abs(p)-(hr-r);",
"  float sd=length(max(q,0.0))+min(max(q.x,q.y),0.0)-r;",
"  float alpha=1.0-smoothstep(-1.25,1.25,sd);",
"  if(alpha<=0.002) discard;",
"  gl_FragColor=vec4(lit,alpha); }"].join("\n");

  function hexToRgb(h) {
    h = String(h).replace("#", "").trim();
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    var n = parseInt(h || "000000", 16);
    return [((n >> 16) & 255)/255, ((n >> 8) & 255)/255, (n & 255)/255];
  }

  function initOne(container) {
    var d = container.dataset;
    var cfg = {
      color1: d.color1 || "#4f7cff", color2: d.color2 || "#ff4fa0",
      highlight: d.highlight || "#ffffff", gridColor: d.gridColor || "#ffffff",
      showGrid: d.showGrid !== "false",
      gridDensity: parseFloat(d.gridDensity || "18"),
      gridOpacity: parseFloat(d.gridOpacity || "0.26"),
      borderRadius: parseFloat(d.borderRadius || "26"),
      stiffness: parseFloat(d.stiffness || "0.05"),
      damping: parseFloat(d.damping || "0.2"),
      grabRadius: parseFloat(d.grabRadius || "0.62"),
      pull: parseFloat(d.pull || "0.55"),
      wobble: parseFloat(d.wobble || "5.5"),
      tilt: parseFloat(d.tilt || "12"),
      shading: parseFloat(d.shading || "0.75"),
      resolution: parseInt(d.resolution || "22", 10),
    };

    var canvas = document.createElement("canvas");
    canvas.style.cssText = "display:block;width:100%;height:100%;";
    container.appendChild(canvas);
    container.style.touchAction = "none";

    var gl = canvas.getContext("webgl", { alpha: true, antialias: true });
    if (!gl) return;   // container's CSS background remains as fallback

    function sh(t, s) {
      var x = gl.createShader(t); gl.shaderSource(x, s); gl.compileShader(x);
      if (!gl.getShaderParameter(x, gl.COMPILE_STATUS)) {
        console.warn("[elastic-mesh]", gl.getShaderInfoLog(x)); return null;
      }
      return x;
    }
    var vs = sh(gl.VERTEX_SHADER, VERT), fs = sh(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn("[elastic-mesh]", gl.getProgramInfoLog(prog)); return;
    }
    gl.useProgram(prog);
    gl.clearColor(0,0,0,0);
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    var N = Math.max(6, Math.min(40, cfg.resolution)), nodeCount = N*N;
    var aGrid = new Float32Array(nodeCount*2), uvA = new Float32Array(nodeCount*2);
    var aOffset = new Float32Array(nodeCount*3), aNormal = new Float32Array(nodeCount*3);
    for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
      var idx = j*N+i, u = i/(N-1), v = j/(N-1);
      aGrid[idx*2]=u; aGrid[idx*2+1]=v; uvA[idx*2]=u; uvA[idx*2+1]=v; aNormal[idx*3+2]=1;
    }
    var index = new Uint16Array((N-1)*(N-1)*6), t = 0;
    for (var jj = 0; jj < N-1; jj++) for (var ii = 0; ii < N-1; ii++) {
      var a = jj*N+ii, b = a+1, c = a+N, dd2 = c+1;
      index[t++]=a; index[t++]=c; index[t++]=b; index[t++]=b; index[t++]=c; index[t++]=dd2;
    }

    function mkbuf(data, usage) {
      var o = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, o);
      gl.bufferData(gl.ARRAY_BUFFER, data, usage);
      return o;
    }
    var bGrid = mkbuf(aGrid, gl.STATIC_DRAW), bUv = mkbuf(uvA, gl.STATIC_DRAW);
    var bOff = mkbuf(aOffset, gl.DYNAMIC_DRAW), bNrm = mkbuf(aNormal, gl.DYNAMIC_DRAW);
    var bIdx = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bIdx);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, index, gl.STATIC_DRAW);

    function attach(name, b, size) {
      var l = gl.getAttribLocation(prog, name); if (l < 0) return;
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.enableVertexAttribArray(l);
      gl.vertexAttribPointer(l, size, gl.FLOAT, false, 0, 0);
    }
    function U(n) { return gl.getUniformLocation(prog, n); }

    gl.uniform3fv(U("uColor1"), hexToRgb(cfg.color1));
    gl.uniform3fv(U("uColor2"), hexToRgb(cfg.color2));
    gl.uniform3fv(U("uHighlight"), hexToRgb(cfg.highlight));
    gl.uniform3fv(U("uGridColor"), hexToRgb(cfg.gridColor));
    gl.uniform1f(U("uGrid"), cfg.showGrid ? 1 : 0);
    gl.uniform1f(U("uGridDensity"), cfg.gridDensity);
    gl.uniform1f(U("uGridOpacity"), cfg.gridOpacity);
    gl.uniform1f(U("uShading"), cfg.shading);
    gl.uniform1f(U("uRadius"), cfg.borderRadius);
    gl.uniform1f(U("uDist"), DIST); gl.uniform1f(U("uFit"), FIT);
    gl.uniform1f(U("uTilt"), cfg.tilt * Math.PI / 180);
    var uAspect = U("uAspect"), uRes = U("uRes");

    var baseX = new Float32Array(nodeCount), baseY = new Float32Array(nodeCount);
    var pos = new Float32Array(nodeCount*3), vel = new Float32Array(nodeCount*3), acc = new Float32Array(nodeCount*3);
    var aspect = 1, dpr = Math.min(window.devicePixelRatio || 1, 2);

    function refreshBase() {
      for (var k = 0; k < nodeCount; k++) {
        baseX[k] = (aGrid[k*2]*2-1)*aspect;
        baseY[k] = 1 - aGrid[k*2+1]*2;
      }
    }
    function resize() {
      var w = container.clientWidth || 1, h = container.clientHeight || 1;
      canvas.width = Math.floor(w*dpr); canvas.height = Math.floor(h*dpr);
      canvas.style.width = w+"px"; canvas.style.height = h+"px";
      gl.viewport(0,0,canvas.width,canvas.height);
      aspect = w/h;
      gl.uniform1f(uAspect, aspect); gl.uniform2f(uRes, w, h);
      refreshBase();
    }
    resize();
    if (window.ResizeObserver) new ResizeObserver(resize).observe(container);
    else window.addEventListener("resize", resize);

    var ptr = { x:0, y:0, tx:0, ty:0, active:false, target:false };
    function toPlane(cx, cy) {
      var r = container.getBoundingClientRect();
      var clipX = ((cx-r.left)/r.width)*2-1, clipY = 1-((cy-r.top)/r.height)*2;
      var tr = cfg.tilt*Math.PI/180, ct = Math.cos(tr), st = Math.sin(tr);
      var aa = clipY/(ct*FIT*DIST);
      var py = (aa*DIST)/(1+aa*st);
      var persp = DIST/(DIST-py*st);
      ptr.tx = (clipX*aspect)/(persp*FIT); ptr.ty = py;
    }
    container.addEventListener("pointermove", function (e) { toPlane(e.clientX, e.clientY); ptr.target = true; });
    container.addEventListener("pointerenter", function () { ptr.target = true; });
    container.addEventListener("pointerleave", function () { ptr.target = false; });
    container.addEventListener("touchmove", function (e) {
      if (e.touches.length) { toPlane(e.touches[0].clientX, e.touches[0].clientY); ptr.target = true; }
    }, { passive: true });
    container.addEventListener("touchend", function () { ptr.target = false; });

    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function substep() {
      var retain = 1 - cfg.damping, coupling = 0.06 + cfg.wobble*0.032;
      var r = Math.max(0.08, cfg.grabRadius)*1.4, invR = 1/r, force = cfg.pull*0.009;
      var live = ptr.active && !reduce;
      for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
        var idx = j*N+i, o3 = idx*3;
        var ox = pos[o3], oy = pos[o3+1], oz = pos[o3+2];
        var ax = -cfg.stiffness*ox, ay = -cfg.stiffness*oy, az = -cfg.stiffness*oz;
        var sx=0, sy=0, sz=0, cnt=0, n;
        if (i>0)   { n=(idx-1)*3; sx+=pos[n]; sy+=pos[n+1]; sz+=pos[n+2]; cnt++; }
        if (i<N-1) { n=(idx+1)*3; sx+=pos[n]; sy+=pos[n+1]; sz+=pos[n+2]; cnt++; }
        if (j>0)   { n=(idx-N)*3; sx+=pos[n]; sy+=pos[n+1]; sz+=pos[n+2]; cnt++; }
        if (j<N-1) { n=(idx+N)*3; sx+=pos[n]; sy+=pos[n+1]; sz+=pos[n+2]; cnt++; }
        ax += coupling*(sx-cnt*ox); ay += coupling*(sy-cnt*oy); az += coupling*(sz-cnt*oz);
        if (live) {
          var dx = ptr.x-(baseX[idx]+ox), dy = ptr.y-(baseY[idx]+oy);
          var dd = Math.sqrt(dx*dx+dy*dy), tn = dd*invR;
          if (tn < 1) {
            var zb = 1-tn*tn;
            az += force*zb*zb*6.0;
            if (dd > 1e-4) {
              var pinch = tn*(1-tn)*(1-tn)*6.75, dir = (force*pinch*1.6)/dd;
              ax += dx*dir; ay += dy*dir;
            }
          }
        }
        acc[o3]=ax; acc[o3+1]=ay; acc[o3+2]=az;
      }
      for (var k = 0; k < nodeCount; k++) {
        var q = k*3;
        var vx=(vel[q]+acc[q])*retain, vy=(vel[q+1]+acc[q+1])*retain, vz=(vel[q+2]+acc[q+2])*retain;
        vel[q]=vx; vel[q+1]=vy; vel[q+2]=vz;
        var px=pos[q]+vx, py=pos[q+1]+vy, pz=pos[q+2]+vz;
        pos[q]   = px >  1.2 ?  1.2 : (px < -1.2 ? -1.2 : px);
        pos[q+1] = py >  1.2 ?  1.2 : (py < -1.2 ? -1.2 : py);
        pos[q+2] = pz >  1.2 ?  1.2 : (pz < -1.2 ? -1.2 : pz);
      }
    }

    function commit() {
      for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
        var idx=j*N+i, o3=idx*3;
        var iL=i>0?idx-1:idx, iR=i<N-1?idx+1:idx, iD=j>0?idx-N:idx, iU=j<N-1?idx+N:idx;
        var lx=baseX[iL]+pos[iL*3], ly=baseY[iL]+pos[iL*3+1], lz=pos[iL*3+2];
        var rx=baseX[iR]+pos[iR*3], ry=baseY[iR]+pos[iR*3+1], rz=pos[iR*3+2];
        var dx=baseX[iD]+pos[iD*3], dy=baseY[iD]+pos[iD*3+1], dz=pos[iD*3+2];
        var ux=baseX[iU]+pos[iU*3], uy=baseY[iU]+pos[iU*3+1], uz=pos[iU*3+2];
        var txx=rx-lx, txy=ry-ly, txz=rz-lz, tyx=ux-dx, tyy=uy-dy, tyz=uz-dz;
        var nx=txy*tyz-txz*tyy, ny=txz*tyx-txx*tyz, nz=txx*tyy-txy*tyx;
        if (nz<0) { nx=-nx; ny=-ny; nz=-nz; }
        var len=Math.sqrt(nx*nx+ny*ny+nz*nz)||1;
        aNormal[o3]=nx/len; aNormal[o3+1]=ny/len; aNormal[o3+2]=nz/len;
        aOffset[o3]=pos[o3]; aOffset[o3+1]=pos[o3+1]; aOffset[o3+2]=pos[o3+2];
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, bOff); gl.bufferSubData(gl.ARRAY_BUFFER, 0, aOffset);
      gl.bindBuffer(gl.ARRAY_BUFFER, bNrm); gl.bufferSubData(gl.ARRAY_BUFFER, 0, aNormal);
    }

    var STEP = 1/120, MAX_SUB = 5, accT = 0, last = performance.now();
    (function frame(now) {
      requestAnimationFrame(frame);
      if (document.hidden) { last = now; return; }
      var dt = (now-last)/1000; last = now; if (dt > 0.25) dt = 0.25;
      var kL = 1-Math.exp(-Math.max(dt,1e-4)/0.06);
      ptr.x += (ptr.tx-ptr.x)*kL; ptr.y += (ptr.ty-ptr.y)*kL;
      ptr.active = ptr.target;
      accT += dt;
      var sub = 0;
      while (accT >= STEP && sub < MAX_SUB) { substep(); accT -= STEP; sub++; }
      if (accT > STEP) accT = 0;
      commit();
      attach("aGrid", bGrid, 2); attach("uv", bUv, 2);
      attach("aOffset", bOff, 3); attach("aNormal", bNrm, 3);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bIdx);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawElements(gl.TRIANGLES, index.length, gl.UNSIGNED_SHORT, 0);
    })(last);
  }

  function init() {
    var nodes = document.querySelectorAll("[data-elastic-mesh]");
    for (var i = 0; i < nodes.length; i++) initOne(nodes[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
