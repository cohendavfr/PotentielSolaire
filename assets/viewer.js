/* Maquette 3D des toits : bâtiments, pans de toiture, ombrage, rues, adresses.
 *
 * startViewer({data, ortho, title, home, mode})
 *   data.buildings[i] : r (contour local), holes, h (hauteur BD TOPO), cx, cy, a, n,
 *     et pour un bâtiment analysé : ratio, az, tilt, y, kwh, kw, shade, sb, sa,
 *     pans: [{pan, pl:[a,b,c], polys:[[ext, trous]], ratio, y, az, tilt, area, kwp, kwh, sh, sb, sa, flat}]
 *     avec z (m au-dessus du sol) = a*x + b*y + c dans le repère local.
 *   data.addresses : [libellé, x, y, indice du bâtiment]   data.streets : [nom, x, y, dx, dy, priorité]
 */
function startViewer(opts) {
  const DATA = opts.data, ORTHO = opts.ortho;
  document.body.classList.add('viewer-on');
  const MODE = opts.mode || 'commune';
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const compass = az => ['N', 'NE', 'E', 'SE', 'S', 'SO', 'O', 'NO'][Math.round(((az % 360) + 360) % 360 / 45) % 8];
  const fmt = n => n >= 1000 ? (n / 1000).toLocaleString('fr-FR', {maximumFractionDigits: 1}) + ' MWh' : Math.round(n) + ' kWh';
  const dark = matchMedia('(prefers-color-scheme:dark)').matches;

  // ------------------------------------------------------------------ interface
  document.body.insertAdjacentHTML('beforeend', `
  <canvas id="c" tabindex="0"></canvas><div id="labels"></div><div id="msg"></div>
  <button id="menuBtn" aria-label="Options">☰</button>
  ${MODE === 'commune' ? `<div id="chip">${esc(opts.title || '')}</div>` : ''}
  <div class="panel" id="side">
    <div><h1>${esc(opts.title || '')}</h1>
      ${opts.home ? `<p class="sub"><a href="${opts.home}" style="color:var(--acc)">${esc(opts.homeLabel || '← Choisir une autre ville')}</a></p>` : ''}
      <p class="sub" style="margin-top:4px">${esc(opts.subtitle || 'Pans de toiture, ombrage des arbres et du bâti. Estimation indicative.')}</p></div>
    ${MODE === 'commune' ? '<div id="res"><input id="q" type="search" placeholder="Chercher une adresse…" autocomplete="off"><div id="sugg"></div></div>' : ''}
    <label>Couleur selon
      <select id="metric" style="flex:1;padding:5px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit">
        <option value="yield">Rendement du pan (kWh/kWc/an)</option>
        <option value="prod">Production du pan (kWh/an)</option>
      </select></label>
    <div><div class="bar" id="bar"></div><div class="scale"><span id="s0"></span><span id="s1"></span><span id="s2"></span></div></div>
    ${MODE === 'commune' ? '<label>Rendement relatif mini <input type="range" id="min" min="0" max="100" value="0" style="flex:1"><b id="minv">0 %</b></label>' : ''}
    <label${MODE === 'address' ? ' style="display:none"' : ''}><span>Noms de rues</span><input type="checkbox" id="streets" checked></label>
    <label${MODE === 'address' ? ' style="display:none"' : ''}><span>Numéros</span><input type="checkbox" id="nums" checked></label>
    <label><span>Bâtiments non analysés</span><input type="checkbox" id="others" checked></label>
    <label><span>Orthophoto</span><input type="checkbox" id="photo" checked></label>
    ${MODE === 'commune' ? '<div id="top"></div>' : '<div id="hint" class="hintin"></div>'}
  </div>
  ${MODE === 'commune' ? '<div class="panel" id="hint"></div>' : ''}
  <div class="panel" id="info"></div>
  <div class="panel" id="nav">
    <button id="rl" title="Tourner à gauche (Q)">⟲</button><button id="rr" title="Tourner à droite (E)">⟳</button>
    <button id="tu" title="Incliner : vue plus rasante (Maj+↑)">⤒</button><button id="td" title="Incliner : vue plus verticale (Maj+↓)">⤓</button>
    <span class="sep"></span>
    <button id="zi" title="Zoom avant (+)">＋</button><button id="zo" title="Zoom arrière (−)">−</button>
    <span class="sep"></span>
    <button id="north" title="Orienter le nord en haut (R)"><svg viewBox="0 0 40 40" id="needle"><circle cx="20" cy="20" r="18" fill="none" stroke="currentColor" opacity=".25"/><path d="M20 5 L25 21 L20 18 L15 21Z" fill="#c0392b"/><path d="M20 35 L25 21 L20 24 L15 21Z" fill="currentColor" opacity=".45"/></svg></button>
    <button id="top2" title="Vue de dessus (T)">⌖</button>
    <span class="sep"></span>
    <button id="mode" title="Basculer le glissé à un doigt : tourner / déplacer">Tourner</button>
  </div>`);

  // ------------------------------------------------------------------ scène
  const canvas = $('c');
  const renderer = new THREE.WebGLRenderer({canvas, antialias: true});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(dark ? 0x14191b : 0xdfe6ea);
  const camera = new THREE.PerspectiveCamera(45, 1, 1, 14000);
  const controls = new THREE.OrbitControls(camera, canvas);
  controls.enableDamping = false; controls.enableZoom = false; controls.enableKeys = false;
  controls.touches = {ONE: THREE.TOUCH.ROTATE, TWO: null};   // les deux doigts sont gérés plus bas
  controls.screenSpacePanning = false; controls.maxPolarAngle = Math.PI * 0.49; controls.minPolarAngle = 0.02;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x667788, 0.8));
  const sunL = new THREE.DirectionalLight(0xfff3dd, 0.85);
  sunL.position.set(-600, 1200, 800);
  scene.add(sunL);

  const groundMat = new THREE.MeshBasicMaterial({color: 0x8a9a8a});
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(DATA.w, DATA.h), groundMat);
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);
  if (ORTHO) new THREE.TextureLoader().load(ORTHO, t => {
    t.anisotropy = renderer.capabilities.getMaxAnisotropy();
    groundMat.map = t; groundMat.color.set(0xffffff); groundMat.needsUpdate = true;
  });

  // ------------------------------------------------------------------ couleurs
  const stops = [[.5, [192, 57, 43]], [.7, [232, 163, 58]], [.82, [242, 214, 75]], [.92, [109, 187, 74]], [1, [31, 157, 85]]];
  function ramp(r) {
    if (r <= stops[0][0]) return new THREE.Color(...stops[0][1].map(v => v / 255));
    for (let i = 1; i < stops.length; i++) if (r <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i], t = (r - a) / (b - a);
      return new THREE.Color(...ca.map((v, k) => (v + (cb[k] - v) * t) / 255));
    }
    return new THREE.Color(...stops[stops.length - 1][1].map(v => v / 255));
  }
  const rampT = t => ramp(0.5 + 0.5 * t);
  const GREY = new THREE.Color(dark ? 0x56606a : 0xb9b4a8);
  const WALL = new THREE.Color(dark ? 0x8a8f94 : 0xf2eee4);
  const NONE = new THREE.Color(dark ? 0x6a5a4e : 0xc9b8a6);
  const WHITE = new THREE.Color(1, 1, 1);
  const XTRA = new THREE.Color(dark ? 0x4c555d : 0xa39e92);   // zones de toit non retenues comme pans
  let metric = 'yield', lo = 0, hi = 1;
  const pval = p => metric === 'yield' ? p.y : Math.log10(Math.max(p.kwh, 1));
  const quant = (arr, p) => arr[Math.floor(p * (arr.length - 1))];
  function setScale() {
    const v = [];
    DATA.buildings.forEach(b => (b.pans || []).forEach(p => { if (metric === 'yield' || p.kwh > 0) v.push(pval(p)); }));
    v.sort((a, b) => a - b);
    if (MODE === 'address' && DATA.meta && DATA.meta.rendement_optimal) {      // échelle fixe : rendements comparables d'une adresse à l'autre
      if (metric === 'yield') { lo = DATA.meta.rendement_optimal * 0.45; hi = DATA.meta.rendement_optimal; }
      else { lo = Math.log10(300); hi = Math.log10(Math.max(v.length ? Math.pow(10, v[v.length - 1]) : 10000, 3000)); }
    } else if (v.length) { lo = quant(v, 0.03); hi = quant(v, 0.97); }
    if (hi - lo < 1e-6) hi = lo + 1;
    $('bar').style.background = `linear-gradient(90deg,${[0, .25, .5, .75, 1].map(t => '#' + rampT(t).getHexString()).join(',')})`;
    if (metric === 'yield') { $('s0').textContent = Math.round(lo / 10) * 10; $('s1').textContent = 'kWh/kWc/an'; $('s2').textContent = Math.round(hi / 10) * 10; }
    else { $('s0').textContent = fmt(Math.pow(10, lo)); $('s1').textContent = 'kWh/an (échelle log.)'; $('s2').textContent = fmt(Math.pow(10, hi)); }
  }
  const panColor = p => (metric === 'prod' && p.kwh <= 0) ? NONE : rampT(clamp((pval(p) - lo) / (hi - lo), 0, 1));

  // ------------------------------------------------------------------ géométrie fusionnée
  const B = DATA.buildings;
  const roof = {pos: [], col: [], idx: [], triB: [], triP: []};
  const wall = {pos: [], col: [], idx: [], triB: []};
  const outl = [];
  const panList = [];                 // {b, k, vs, vn, cx, cz, cy}
  const rRange = [], wRange = [];     // plages d'indices par bâtiment : [début, nombre]
  const ringArea = r => { let s = 0; for (let i = 0; i < r.length; i++) { const p = r[i], q = r[(i + 1) % r.length]; s += p[0] * q[1] - q[0] * p[1]; } return s / 2; };
  function pip(x, y, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function addPoly(ext, holes, zf, bi, pi, color) {
    const V = ext.map(p => new THREE.Vector2(p[0], p[1]));
    const H = (holes || []).map(h => h.map(p => new THREE.Vector2(p[0], p[1])));
    const all = V.concat(...H);
    const faces = THREE.ShapeUtils.triangulateShape(V.slice(), H.map(h => h.slice()));
    const base = roof.pos.length / 3;
    all.forEach(v => { roof.pos.push(v.x, zf(v.x, v.y), -v.y); roof.col.push(color.r, color.g, color.b); });
    faces.forEach(f => { roof.idx.push(base + f[0], base + f[1], base + f[2]); roof.triB.push(bi); roof.triP.push(pi); });
    const ring = (pts, off) => pts.forEach((p, i) => {
      const q = pts[(i + 1) % pts.length];
      outl.push(p[0], zf(p[0], p[1]) + 0.05, -p[1], q[0], zf(q[0], q[1]) + 0.05, -q[1]);
    });
    if (pi >= 0) { ring(ext); (holes || []).forEach(h => ring(h)); }
    return [base, all.length];
  }
  function addWallQuad(x1, y1, z1, x2, y2, z2, bi) {
    const base = wall.pos.length / 3;
    wall.pos.push(x1, 0, -y1, x1, z1, -y1, x2, 0, -y2, x2, z2, -y2);
    for (let k = 0; k < 4; k++) wall.col.push(WALL.r, WALL.g, WALL.b);
    wall.idx.push(base, base + 2, base + 1, base + 1, base + 2, base + 3);
    wall.triB.push(bi, bi);
  }
  const planeZ = (pl, x, y, zr) => { const z = pl[0] * x + pl[1] * y + pl[2]; return zr ? clamp(z, zr[0], zr[1]) : z; };

  B.forEach((b, bi) => {
    const r0 = roof.idx.length, w0 = wall.idx.length;
    if (b.pans && b.pans.length) {
      b.pans.forEach((p, k) => {
        const gi = panList.length;
        const rec = {b: bi, k, vs: [], cx: 0, cy: 0, cz: 0, ar: 0};
        p.polys.forEach(([ext, holes]) => {
          const zf = (x, y) => Math.max(planeZ(p.pl, x, y, p.zr), 0.3);
          rec.vs.push(addPoly(ext, holes, zf, bi, gi, GREY));
          const e = ext.reduce((s, q) => [s[0] + q[0], s[1] + q[1]], [0, 0]);
          const cx = e[0] / ext.length, cy = e[1] / ext.length, ar = Math.abs(ringArea(ext));
          rec.cx += cx * ar; rec.cy += cy * ar; rec.ar += ar;
        });
        rec.cx /= rec.ar; rec.cy /= rec.ar; rec.cz = Math.max(planeZ(p.pl, rec.cx, rec.cy, p.zr), 0.3);
        panList.push(rec);
      });
      (b.xtra || []).forEach(x => {
        const gi = panList.length, rec = {b: bi, k: -2, vs: [], cx: 0, cy: 0, cz: 0};
        x.polys.forEach(([ext, holes]) => rec.vs.push(addPoly(ext, holes, (xx, yy) => Math.max(planeZ(x.pl, xx, yy, x.zr), 0.3), bi, gi, GREY)));
        panList.push(rec);
      });
      // murs : suivent la surface du toit le long de l'emprise
      const ring = b.r, ccw = ringArea(ring) > 0;
      for (let i = 0; i < ring.length; i++) {
        const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % ring.length];
        const L = Math.hypot(x2 - x1, y2 - y1); if (L < 0.05) continue;
        const nx = (ccw ? -(y2 - y1) : (y2 - y1)) / L, ny = (ccw ? (x2 - x1) : -(x2 - x1)) / L;
        const m = Math.max(1, Math.ceil(L / 2)); let prev = null;
        for (let s = 0; s <= m; s++) {
          const t = s / m, x = x1 + (x2 - x1) * t, y = y1 + (y2 - y1) * t;
          let z = null;
          for (const p of b.pans.concat(b.xtra || [])) { for (const [ext] of p.polys) if (pip(x + nx * 0.4, y + ny * 0.4, ext)) { z = planeZ(p.pl, x, y, p.zr); break; } if (z !== null) break; }
          if (z === null) {   // hors des polygones : plan du pan dont un sommet est le plus proche
            let best = null, bd = 1e9;
            for (const p of b.pans.concat(b.xtra || [])) for (const [ext] of p.polys) for (const q of ext) { const d = (q[0] - x) ** 2 + (q[1] - y) ** 2; if (d < bd) { bd = d; best = p; } }
            z = planeZ(best.pl, x, y, best.zr);
          }
          z = Math.max(z, 0.5);
          if (prev) addWallQuad(prev[0], prev[1], prev[2], x, y, z, bi);
          prev = [x, y, z];
        }
      }
    } else {
      const gi = panList.length;
      panList.push({b: bi, k: -1, vs: [addPoly(b.r, b.holes, () => b.h, bi, -2 - bi, GREY)], cx: b.cx, cy: b.cy, cz: b.h});
      b.r.forEach((p, i) => { const q = b.r[(i + 1) % b.r.length]; addWallQuad(p[0], p[1], b.h, q[0], q[1], b.h, bi); });
    }
    rRange[bi] = [r0, roof.idx.length - r0]; wRange[bi] = [w0, wall.idx.length - w0];
  });

  function makeMesh(g, withTri) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(g.pos, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(g.col, 3));
    geo.setIndex(new THREE.BufferAttribute(new Uint32Array(g.idx), 1));
    geo.computeVertexNormals(); geo.computeBoundingSphere();
    const mat = new THREE.MeshLambertMaterial({vertexColors: true, side: THREE.DoubleSide});
    const m = new THREE.Mesh(geo, mat); scene.add(m); return m;
  }
  const roofMesh = makeMesh(roof), wallMesh = makeMesh(wall);
  let curRoofTri = {b: Int32Array.from(roof.triB), p: Int32Array.from(roof.triP)}, curWallTri = Int32Array.from(wall.triB);
  const lineGeo = new THREE.BufferGeometry();
  lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(outl, 3));
  const lines = new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({color: dark ? 0xffffff : 0x10242b, transparent: true, opacity: 0.55}));
  scene.add(lines);
  const selLineGeo = new THREE.BufferGeometry();
  const selLines = new THREE.LineSegments(selLineGeo, new THREE.LineBasicMaterial({color: 0xffa000, linewidth: 3, depthTest: false}));
  selLines.renderOrder = 5; scene.add(selLines);

  // ------------------------------------------------------------------ état
  let selB = -1, selP = -1;          // bâtiment et pan sélectionnés (indices locaux)
  const bestPan = b => b.pans && b.pans.length ? b.pans[0] : null;
  const analysed = B.map((b, i) => i).filter(i => B[i].pans && B[i].pans.length);
  let ranked = analysed.slice();

  function recolor() {
    const col = roofMesh.geometry.attributes.color;
    panList.forEach((rec, gi) => {
      const b = B[rec.b];
      let c = rec.k === -2 ? XTRA : rec.k < 0 ? GREY : panColor(b.pans[rec.k]);
      if (rec.b === selB) c = c.clone().lerp(WHITE, (rec.k === selP && rec.k >= 0) ? 0.5 : 0.14);
      rec.vs.forEach(([vs, vn]) => { for (let v = vs; v < vs + vn; v++) col.setXYZ(v, c.r, c.g, c.b); });
    });
    col.needsUpdate = true;
    const wc = wallMesh.geometry.attributes.color;
    B.forEach((b, bi) => {
      const c = bi === selB ? WALL.clone().lerp(new THREE.Color(0xffd27a), 0.35) : WALL;
      const seen = new Set();
      for (let i = wRange[bi][0]; i < wRange[bi][0] + wRange[bi][1]; i++) { const v = wall.idx[i]; if (!seen.has(v)) { seen.add(v); wc.setXYZ(v, c.r, c.g, c.b); } }
    });
    wc.needsUpdate = true;
    // contour du pan sélectionné
    const seg = [];
    if (selB >= 0 && selP >= 0) {
      const p = B[selB].pans[selP];
      p.polys.forEach(([ext, holes]) => [ext, ...(holes || [])].forEach(ring => ring.forEach((a, i) => {
        const q = ring[(i + 1) % ring.length], zf = (x, y) => Math.max(planeZ(p.pl, x, y, p.zr), 0.3) + 0.12;
        seg.push(a[0], zf(a[0], a[1]), -a[1], q[0], zf(q[0], q[1]), -q[1]);
      })));
    }
    selLineGeo.setAttribute('position', new THREE.Float32BufferAttribute(seg, 3));
  }

  function setMetric(k) {
    metric = k; setScale();
    const key = k === 'yield' ? (b => b.y) : (b => b.kwh);
    ranked = analysed.slice().sort((a, b) => key(B[b]) - key(B[a]) || B[b].kwh - B[a].kwh);
    ranked.forEach((bi, i) => B[bi].rank = i + 1);
    recolor(); applyFilters(); if (selB >= 0) showInfo();
  }

  // filtres : on reconstruit les index des triangles visibles
  function applyFilters() {
    const min = $('min') ? +$('min').value / 100 : 0;
    const others = $('others').checked;
    if ($('minv')) $('minv').textContent = Math.round(min * 100) + ' %';
    const vis = B.map(b => b.pans && b.pans.length ? b.ratio >= min : others);
    function rebuild(mesh, g, ranges, triArrs) {
      let n = 0; vis.forEach((v, i) => { if (v) n += ranges[i][1]; });
      const idx = new Uint32Array(n), tb = new Int32Array(n / 3), tp = triArrs.p ? new Int32Array(n / 3) : null;
      let o = 0, t = 0;
      vis.forEach((v, i) => {
        if (!v) return; const [s, c] = ranges[i];
        for (let k = 0; k < c; k++) idx[o++] = g.idx[s + k];
        for (let k = 0; k < c / 3; k++) { tb[t] = triArrs.srcB[s / 3 + k]; if (tp) tp[t] = triArrs.srcP[s / 3 + k]; t++; }
      });
      mesh.geometry.setIndex(new THREE.BufferAttribute(idx, 1));
      return {b: tb, p: tp};
    }
    curRoofTri = rebuild(roofMesh, roof, rRange, {p: true, srcB: roof.triB, srcP: roof.triP});
    const w = rebuild(wallMesh, wall, wRange, {srcB: wall.triB});
    curWallTri = w.b;
    // contours des pans : seulement les bâtiments visibles
    let nl = 0; vis.forEach((vv, i) => { if (vv) nl += outlineByB[i][1]; });
    const lp = new Float32Array(nl); let lo2 = 0;
    vis.forEach((vv, i) => { if (!vv) return; const [s0, c] = outlineByB[i]; for (let k = 0; k < c; k++) lp[lo2++] = outl[s0 + k]; });
    lineGeo.setAttribute('position', new THREE.BufferAttribute(lp, 3));
    renderTop();
    if (selB >= 0 && !vis[selB]) select(-1, -1);
  }
  const outlineByB = [];  // positions de contour par bâtiment (pour filtrer)
  // contours : ré-indexation par bâtiment (construit une fois)
  (function () {
    let o = 0;
    B.forEach((b, bi) => { outlineByB[bi] = []; });
    // outl a été rempli dans l'ordre des bâtiments : on mesure par bâtiment
    let pos = 0;
    B.forEach((b, bi) => {
      let n = 0;
      if (b.pans) b.pans.concat(b.xtra || []).forEach(p => p.polys.forEach(([ext, holes]) => { n += ext.length + (holes || []).reduce((s, h) => s + h.length, 0); }));
      outlineByB[bi] = [pos, n * 6]; pos += n * 6;
    });
  })();

  // ------------------------------------------------------------------ liste (commune) et fiche
  const topEl = $('top');
  function renderTop() {
    if (!topEl) return;
    const min = +$('min').value / 100;
    const list = ranked.filter(bi => B[bi].ratio >= min);
    topEl.innerHTML = `<p class="sub" style="margin-bottom:6px">${list.length} bâtiments · top 25</p>` + list.slice(0, 25).map(bi => {
      const b = B[bi], p = bestPan(b), c = '#' + panColor(p).getHexString();
      return `<div class="row" data-i="${bi}"><i class="dot" style="background:${c}"></i><b>#${b.rank}</b>
        <span>${Math.round(b.y)} kWh/kWc</span><span class="meta">${b.pans.length} pan${b.pans.length > 1 ? 's' : ''} · ${fmt(b.kwh)}/an</span>
        ${b.a ? `<span class="ad">${esc(b.a)}</span>` : ''}</div>`;
    }).join('');
  }
  if (topEl) topEl.addEventListener('click', e => { const r = e.target.closest('.row'); if (r) select(+r.dataset.i, -1, true); });

  const info = $('info');

  // ------------------------------------------------------------------ bilan économique
  // Hypothèses par défaut (octobre 2026) — modifiables dans le panneau, à vérifier avant toute décision :
  //  - prix du kWh : tarif réglementé de vente d'électricité, option Base, 6 kVA : 0,2001 € TTC/kWh
  //    (délibération CRE du 15 juillet 2026, hausse du 1er août 2026) ;
  //  - surplus injecté : 1,1 c€/kWh HT pour une installation ≤ 9 kWc, indexé de 2 % par an
  //    (arrêté du 1er juin 2026, JO du 4 juin 2026) ; la prime à l'autoconsommation a été supprimée ;
  //  - coût de l'installation : ordre de grandeur 2026 pour du résidentiel posé, TTC (TVA 5,5 % jusqu'à 9 kWc) :
  //    forfait + prix par kWc, d'où environ 7 400 € pour 3 kWc, 11 400 € pour 6 kWc, 15 500 € pour 9 kWc.
  const DEFAULT_SHARE = 0.6;                          // part du maximum proposée par défaut
  const MODULE_KWC = 0.43;                            // module de 430 Wc
  const ECO0 = {prixKwh: 0.2001, surplus: 0.011, conso: 5000, scAuto: true, sc: 40, fixe: 3300, parKwc: 1350, prime: 0};
  const ECO = Object.assign({}, ECO0);
  try { Object.assign(ECO, JSON.parse(localStorage.getItem('eco-v1') || '{}')); } catch (e) {}
  const saveEco = () => { try { localStorage.setItem('eco-v1', JSON.stringify(ECO)); } catch (e) {} };
  const equip = {};                                   // bâtiment -> {pan: nombre de panneaux retenus}
  const euro = n => Math.round(n).toLocaleString('fr-FR') + ' €';
  const equipOf = bi => {
    if (!equip[bi]) { equip[bi] = {}; B[bi].pans.forEach((p, k) => { equip[bi][k] = (p.ratio >= 0.85 && p.n > 0) ? Math.max(1, Math.round(DEFAULT_SHARE * p.n)) : 0; }); }
    return equip[bi];
  };
  function bilan(bi) {
    const sel = equipOf(bi), ps = B[bi].pans.map((p, k) => ({p, n: sel[k] || 0})).filter(x => x.n > 0);
    const n = ps.reduce((s, x) => s + x.n, 0), kwc = n * MODULE_KWC, prod = ps.reduce((s, x) => s + x.n * MODULE_KWC * x.p.y, 0);
    const area = n * 1.94;
    const conso = Math.max(ECO.conso, 1);
    let sc = ECO.scAuto ? clamp(0.38 * Math.pow(conso / Math.max(prod, 1), 0.7), 0.1, 0.85) : clamp(ECO.sc / 100, 0, 1);
    const auto = Math.min(sc * prod, conso * 0.9), inj = Math.max(prod - auto, 0);
    const ecoFacture = auto * ECO.prixKwh, revSurplus = inj * ECO.surplus, eco = ecoFacture + revSurplus;
    const brut = kwc > 0 ? (ECO.fixe + ECO.parKwc * kwc) * (kwc > 9 ? 1.20 / 1.055 : 1) : 0;
    const net = Math.max(brut - (ECO.prime || 0) * kwc, 0);
    return {n, kwc, prod, area, sc: prod > 0 ? auto / prod : 0, auto, inj, ecoFacture, revSurplus, eco, brut, net, retour: eco > 0 ? net / eco : Infinity};
  }
  function bilanHtml(bi) {
    const r = bilan(bi), small = r.kwc > 0 && r.kwc < 1.5;
    const num = (id, label, v, step) => `<label class="eco-l">${label}<input class="eco-in" id="${id}" type="number" step="${step}" value="${v}"></label>`;
    return `<h3>Bilan économique · panneaux retenus</h3>
      ${r.n === 0 ? '<p class="note">Indiquez un nombre de panneaux dans le tableau pour chiffrer l\'installation.</p>' : `<dl class="bil">
        <dt>Panneaux (430 Wc)</dt><dd><b>${r.n}</b> · ${r.kwc.toFixed(2)} kWc · ${Math.round(r.area)} m²</dd>
        <dt>Production</dt><dd>${Math.round(r.prod).toLocaleString('fr-FR')} kWh/an</dd>
        <dt>Autoconsommé</dt><dd>${Math.round(r.auto).toLocaleString('fr-FR')} kWh (${Math.round(r.sc * 100)} %)</dd>
        <dt>Injecté (surplus)</dt><dd>${Math.round(r.inj).toLocaleString('fr-FR')} kWh</dd>
        <dt>Économie sur la facture</dt><dd>${euro(r.ecoFacture)}/an</dd>
        <dt>Vente du surplus</dt><dd>${euro(r.revSurplus)}/an</dd>
        <dt class="tot">Gain annuel</dt><dd class="tot">${euro(r.eco)}/an</dd>
        <dt>Investissement estimé (TTC)</dt><dd>${euro(r.net)}</dd>
        <dt>Retour sur investissement</dt><dd>${isFinite(r.retour) ? r.retour.toFixed(1).replace('.', ',') + ' ans' : '—'}</dd></dl>
        ${small ? '<p class="note">Très petite installation : le coût fixe (pose, raccordement, onduleur) pèse lourd, le retour est peu représentatif.</p>' : ''}`}
      <details id="ecoDet"${info.dataset.ecoOpen ? ' open' : ''}><summary>Hypothèses (modifiables)</summary>
        ${num('e-conso', 'Consommation du foyer (kWh/an)', ECO.conso, 100)}
        <label class="eco-l">Autoconsommation <span><input id="e-auto" type="checkbox"${ECO.scAuto ? ' checked' : ''}> estimée</span>
          <input class="eco-in" id="e-sc" type="number" step="5" min="0" max="100" value="${ECO.sc}"${ECO.scAuto ? ' disabled' : ''}> %</label>
        ${num('e-prix', 'Prix du kWh évité (€ TTC)', ECO.prixKwh, 0.005)}
        ${num('e-surplus', 'Tarif du surplus (€/kWh)', ECO.surplus, 0.005)}
        ${num('e-fixe', 'Coût fixe de l\'installation (€ TTC)', ECO.fixe, 100)}
        ${num('e-kwc', 'Coût par kWc (€ TTC)', ECO.parKwc, 50)}
        ${num('e-prime', 'Aide à l\'investissement (€/kWc)', ECO.prime, 10)}
        <p class="note">Prix du kWh : tarif réglementé Base 6 kVA, 0,2001 € TTC (<a href="https://selectra.info/energie/electricite/prix/trv" target="_blank">CRE, août 2026</a>). Surplus ≤ 9 kWc : 1,1 c€/kWh HT, aucune prime à l'autoconsommation (arrêté du 1<sup>er</sup> juin 2026, <a href="https://www.artyseo.fr/blog/reforme-s21-juin-2026-panneaux-solaires/" target="_blank">détail</a>) ; certaines sources donnent encore 4 c€ : vérifiez auprès d'EDF OA. Coûts : ordres de grandeur 2026, pose comprise, TVA 5,5 % jusqu'à 9 kWc (<a href="https://www.potentielsolaire.com/prix-panneaux-solaires" target="_blank">sources</a>). Hors batterie, dégradation des modules (~0,4 %/an), maintenance et remplacement de l'onduleur. Devis RGE à demander.</p>
        <button class="btn" id="e-reset" style="margin-top:4px">Valeurs par défaut</button>
      </details>`;
  }
  function refreshBilan() {
    const box = $('bilanBox'); if (!box || selB < 0) return;
    info.dataset.ecoOpen = $('ecoDet') && $('ecoDet').open ? '1' : '';
    box.innerHTML = bilanHtml(selB);
    if ($('peekSum')) $('peekSum').textContent = peekText(selB);
    wireEco();
  }
  function wireEco() {
    const f = (id, key, scale) => { const el = $(id); if (el) el.oninput = () => { const v = parseFloat(el.value); if (isFinite(v)) { ECO[key] = v * (scale || 1); saveEco(); const keep = el.id, pos = el.selectionStart; refreshBilan(); const e2 = $(keep); if (e2) { e2.focus(); try { e2.setSelectionRange(pos, pos); } catch (x) {} } } }; };
    f('e-conso', 'conso'); f('e-sc', 'sc'); f('e-prix', 'prixKwh'); f('e-surplus', 'surplus'); f('e-fixe', 'fixe'); f('e-kwc', 'parKwc'); f('e-prime', 'prime');
    if ($('e-auto')) $('e-auto').onchange = e => { ECO.scAuto = e.target.checked; saveEco(); refreshBilan(); };
    if ($('e-reset')) $('e-reset').onclick = () => { Object.assign(ECO, ECO0); saveEco(); refreshBilan(); };
  }

  let infoPeek = true;                                 // mobile : fiche réduite à un résumé
  const isMob = () => matchMedia('(max-width:760px)').matches;
  const ihead = '<div class="ihead"><button id="infoToggle" class="ibtn" aria-label="Agrandir / réduire">▴</button><button id="infoClose" class="ibtn" aria-label="Fermer">✕</button></div>';
  function wireSheet() {
    info.classList.toggle('peek', isMob() && infoPeek);
    $('infoToggle').onclick = () => { infoPeek = !infoPeek; info.classList.toggle('peek', isMob() && infoPeek); $('infoToggle').textContent = infoPeek ? '▴' : '▾'; };
    $('infoClose').onclick = () => select(-1, -1);
    $('infoToggle').textContent = infoPeek ? '▴' : '▾';
  }
  const peekText = bi => { const r = bilan(bi); return r.n ? `${r.n} panneaux · ${r.kwc.toFixed(1)} kWc · gain ${euro(r.eco)}/an` : `${B[bi].pans.length} pans · touchez ▴ pour le détail`; };
  function showInfo() {
    if (selB < 0) { info.style.display = 'none'; return; }
    const b = B[selB];
    info.style.display = 'block';
    const ad = b.a ? `<p class="ad">${esc(b.a)}</p>` : '';
    if (!b.pans || !b.pans.length) {
      info.innerHTML = `${ihead}<h2>Bâtiment non analysé</h2>${ad}<p class="note">Hauteur ${b.h} m.${opts.onAnalyse ? '' : ' Pas de pan de toit exploitable (surface trop petite ou LiDAR indisponible).'}</p>
        ${opts.onAnalyse && b.id ? '<button id="anaBtn" class="btn">Analyser ce bâtiment</button>' : ''}`;
      if ($('anaBtn')) $('anaBtn').onclick = () => opts.onAnalyse(b.id);
      wireSheet(); info.classList.remove('peek');
      return;
    }
    const set = equipOf(selB);
    const rows = b.pans.map((p, k) => `<tr class="pr${k === selP ? ' sel' : ''}" data-k="${k}">
        <td><span class="pdot" style="background:#${panColor(p).getHexString()}"></span>${p.pan}</td>
        <td>${compass(p.az)} ${Math.round(p.az)}° · ${Math.round(p.tilt)}°</td>
        <td class="npan"><input type="number" class="eq" data-k="${k}" min="0" max="${p.n}" value="${set[k] || 0}"${p.n > 0 ? '' : ' disabled'} title="Nombre de panneaux retenus (maximum ${p.n})"> / ${p.n}</td>
        <td>${(p.sh * 100).toFixed(0)} %</td><td>${Math.round(p.y)}</td><td class="kwhc" data-k="${k}">${Math.round((set[k] || 0) * MODULE_KWC * p.y).toLocaleString('fr-FR')}</td></tr>`).join('');
    let detail = '';
    if (selP >= 0) {
      const p = b.pans[selP];
      detail = `<h3>Pan ${p.pan}${p.flat ? ' · toit plat' : ''}</h3><dl>
        <dt>Orientation</dt><dd>${Math.round(p.az)}° (${compass(p.az)})</dd><dt>Pente</dt><dd>${Math.round(p.tilt)}°</dd>
        <dt>Surface du pan</dt><dd>${Math.round(p.area)} m²</dd><dt>Panneaux possibles</dt><dd><b>${p.n}</b> · ${p.kwp.toFixed(2)} kWc</dd>
        <dt>Rendement</dt><dd>${Math.round(p.y)} kWh/kWc/an (${Math.round(p.ratio * 100)} %)</dd>
        <dt>Production</dt><dd>${Math.round(p.kwh).toLocaleString('fr-FR')} kWh/an</dd>
        <dt>Ombrage du pan</dt><dd>${(p.sh * 100).toFixed(1)} %</dd><dt>· dont arbres</dt><dd>${(p.sa * 100).toFixed(1)} %</dd>
        <dt>· dont bâti</dt><dd>${(p.sb * 100).toFixed(1)} %</dd></dl>`;
    }
    const totN = b.pans.reduce((s, p) => s + p.n, 0);
    info.innerHTML = `${ihead}<h2>${MODE === 'commune' ? 'Bâtiment #' + b.rank : 'Bâtiment analysé'} · ${b.pans.length} pan${b.pans.length > 1 ? 's' : ''}</h2>${ad}<p class="peeksum" id="peekSum">${peekText(selB)}</p>
      <dl><dt>Meilleur pan</dt><dd>${Math.round(b.y)} kWh/kWc/an (${Math.round(b.ratio * 100)} %)</dd>
      <dt>Panneaux possibles (tous pans)</dt><dd>${totN}</dd><dt>Hauteur</dt><dd>${b.h} m</dd></dl>
      ${detail}
      <table class="pans"><thead><tr><th>Pan</th><th>Orient. · pente</th><th title="Nombre de panneaux retenus / maximum possible">Panneaux</th><th>Ombre</th><th>kWh/kWc</th><th title="Production des panneaux retenus">kWh/an</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="note">Modifiez le nombre de panneaux de chaque pan (maximum = surface équipable, 85 % du pan). Par défaut, 60 % du maximum est retenu sur les pans à 85 % ou plus du rendement optimal. Cliquez un pan pour le détail.</p>
      <div id="bilanBox">${bilanHtml(selB)}</div>`;
    wireEco(); wireSheet();
  }
  info.addEventListener('click', e => {
    if (e.target.closest('input.eq')) return;           // la saisie du nombre de panneaux ne sélectionne pas le pan
    const r = e.target.closest('tr.pr'); if (r) select(selB, selP === +r.dataset.k ? -1 : +r.dataset.k, false);
  });
  info.addEventListener('input', e => {
    const inp = e.target.closest('input.eq'); if (!inp) return;
    const k = +inp.dataset.k, p = B[selB].pans[k];
    const v = clamp(Math.round(parseFloat(inp.value) || 0), 0, p.n);
    equipOf(selB)[k] = v;
    const cell = info.querySelector(`td.kwhc[data-k="${k}"]`);
    if (cell) cell.textContent = Math.round(v * MODULE_KWC * p.y).toLocaleString('fr-FR');
    refreshBilan();
  });
  info.addEventListener('change', e => {                // valeur hors limites : on remet dans [0, maximum]
    const inp = e.target.closest('input.eq'); if (!inp) return;
    const k = +inp.dataset.k; inp.value = equipOf(selB)[k];
  });

  // pastilles numérotées des pans du bâtiment sélectionné
  const layer = $('labels');
  let panEls = [];
  $('menuBtn').onclick = () => document.body.classList.toggle('side-open');
  canvas.addEventListener('pointerdown', () => document.body.classList.remove('side-open'));   // toucher la carte referme le menu
  function select(bi, pk, fly) {
    if (bi >= 0) document.body.classList.remove('side-open');
    if (bi !== selB) infoPeek = true;
    selB = bi; selP = pk === undefined ? -1 : pk;
    panEls.forEach(e => e.el.remove()); panEls = [];
    if (bi >= 0 && B[bi].pans) B[bi].pans.forEach((p, k) => {
      const rec = panList.find(r => r.b === bi && r.k === k);
      const el = document.createElement('div'); el.className = 'pn'; el.textContent = p.pan; layer.appendChild(el);
      panEls.push({el, rec});
    });
    recolor(); showInfo();
    if (fly && bi >= 0) flyTo(new THREE.Vector3(B[bi].cx, B[bi].h, -B[bi].cy), 70, 0.95);
  }

  // clic : sélection du pan / bâtiment
  const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
  let down = null;
  canvas.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; canvas.focus(); });
  canvas.addEventListener('pointerup', e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    mouse.set(e.clientX / innerWidth * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObjects([roofMesh, wallMesh])[0];
    if (!hit) { select(-1, -1); return; }
    if (hit.object === roofMesh) {
      const bi = curRoofTri.b[hit.faceIndex], gi = curRoofTri.p[hit.faceIndex];
      const k = gi >= 0 && panList[gi].k >= 0 ? panList[gi].k : -1;
      select(bi, bi === selB && k === selP ? -1 : k);
    } else select(curWallTri[hit.faceIndex], -1);
  });

  // ------------------------------------------------------------------ navigation (pavé tactile Mac)
  const sph = new THREE.Spherical(), off = new THREE.Vector3();
  function getView() { off.copy(camera.position).sub(controls.target); sph.setFromVector3(off); return sph; }
  function setView(s) {
    s.phi = clamp(s.phi, 0.02, Math.PI * 0.49); s.radius = clamp(s.radius, 12, 5500); s.makeSafe();
    off.setFromSpherical(s); camera.position.copy(controls.target).add(off); camera.lookAt(controls.target);
  }
  const rotate = (dT, dP) => { const s = getView(); s.theta += dT; s.phi += dP; setView(s); };
  const dolly = f => { const s = getView(); s.radius *= f; setView(s); };
  const bound = Math.max(DATA.w, DATA.h) / 2;
  function pan(dx, dy) {
    const s = getView(), az = s.theta;
    controls.target.x = clamp(controls.target.x + Math.cos(az) * dx - Math.sin(az) * dy, -bound, bound);
    controls.target.z = clamp(controls.target.z - Math.sin(az) * dx - Math.cos(az) * dy, -bound, bound);
    setView(s);
  }
  let padMode = true, dragPan = false;
  const modeBtn = $('mode');
  function setDrag(p) {
    dragPan = p;
    controls.mouseButtons.LEFT = p ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE;
    controls.mouseButtons.RIGHT = p ? THREE.MOUSE.ROTATE : THREE.MOUSE.PAN;
    controls.touches.ONE = p ? THREE.TOUCH.PAN : THREE.TOUCH.ROTATE;
    modeBtn.textContent = p ? 'Déplacer' : 'Tourner'; modeBtn.classList.toggle('on', p); hint();
  }
  modeBtn.addEventListener('click', () => setDrag(!dragPan));
  function hint() {
    $('hint').innerHTML = padMode
      ? `<b>Pavé tactile</b> : deux doigts = <b>déplacer</b> · pincer = <b>zoom</b> · <b>⌥ Option</b> + deux doigts = <b>tourner / incliner</b> · glisser à un doigt (clic enfoncé) = ${dragPan ? 'déplacer' : 'tourner'}.<br>Clavier : flèches déplacer · + / − zoom · Q / E tourner · R nord · T dessus. <a href="#" id="swm" style="color:var(--acc)">J'utilise une souris</a>`
      : `<b>Souris</b> : molette = <b>zoom</b> · glisser = ${dragPan ? 'déplacer' : 'tourner'} · clic droit = ${dragPan ? 'tourner' : 'déplacer'}.<br>Clavier : flèches · + / − · Q / E · R nord · T dessus. <a href="#" id="swm" style="color:var(--acc)">J'utilise un pavé tactile</a>`;
    $('swm').addEventListener('click', e => { e.preventDefault(); padMode = !padMode; hint(); });
  }
  // deux doigts : pincer = zoom, glisser = déplacer, torsion = tourner
  let tp = null;
  const two = ts => { const a = ts[0], b = ts[1]; return {d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2, r: Math.atan2(b.clientY - a.clientY, b.clientX - a.clientX)}; };
  canvas.addEventListener('touchstart', e => { if (e.touches.length === 2) { tp = two(e.touches); anim = null; } else tp = null; }, {passive: false});
  canvas.addEventListener('touchmove', e => {
    if (e.touches.length !== 2 || !tp) return;
    e.preventDefault();
    const t = two(e.touches), s = getView();
    dolly(tp.d / Math.max(t.d, 1));
    const k = s.radius * 0.0021;
    pan(-(t.x - tp.x) * k, (t.y - tp.y) * k);
    let dr = t.r - tp.r; dr = Math.atan2(Math.sin(dr), Math.cos(dr));
    rotate(-dr, 0);
    tp = t;
  }, {passive: false});
  canvas.addEventListener('touchend', e => { if (e.touches.length < 2) tp = null; });

  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    let dx = e.deltaX, dy = e.deltaY; if (e.deltaMode === 1) { dx *= 16; dy *= 16; }
    const s = getView();
    if (e.ctrlKey) dolly(Math.exp(dy * 0.012));
    else if (!padMode) dolly(Math.exp(dy * 0.0015));
    else if (e.altKey || e.shiftKey) rotate(-dx * 0.006, -dy * 0.006);
    else { const k = s.radius * 0.0011; pan(dx * k, -dy * k); }
  }, {passive: false});
  let g0 = null;
  canvas.addEventListener('gesturestart', e => { e.preventDefault(); g0 = {s: e.scale, r: e.rotation}; });
  canvas.addEventListener('gesturechange', e => { e.preventDefault(); if (!g0) return; dolly(g0.s / e.scale); rotate(-(e.rotation - g0.r) * Math.PI / 180, 0); g0 = {s: e.scale, r: e.rotation}; });
  canvas.addEventListener('gestureend', e => { e.preventDefault(); g0 = null; });

  let anim = null;
  function flyTo(target, dist, phi, theta) {
    const s = getView();
    anim = {t0: performance.now(), dur: 650, fromT: controls.target.clone(), toT: target.clone(), fromR: s.radius, toR: dist ?? s.radius,
            fromP: s.phi, toP: phi ?? s.phi, fromTh: s.theta, toTh: theta === undefined ? s.theta : theta};
    if (theta !== undefined) { let d = anim.toTh - anim.fromTh; d = Math.atan2(Math.sin(d), Math.cos(d)); anim.toTh = anim.fromTh + d; }
  }
  function stepAnim() {
    if (!anim) return;
    const t = (performance.now() - anim.t0) / anim.dur, e = t >= 1 ? 1 : 1 - Math.pow(1 - t, 3);
    controls.target.lerpVectors(anim.fromT, anim.toT, e);
    const s = getView();
    s.radius = anim.fromR + (anim.toR - anim.fromR) * e; s.phi = anim.fromP + (anim.toP - anim.fromP) * e; s.theta = anim.fromTh + (anim.toTh - anim.fromTh) * e;
    setView(s); if (t >= 1) anim = null;
  }
  controls.addEventListener('start', () => anim = null);
  canvas.addEventListener('wheel', () => anim = null, {passive: true});
  $('rl').onclick = () => flyTo(controls.target, undefined, undefined, getView().theta + Math.PI / 4);
  $('rr').onclick = () => flyTo(controls.target, undefined, undefined, getView().theta - Math.PI / 4);
  $('tu').onclick = () => rotate(0, 0.2); $('td').onclick = () => rotate(0, -0.2);
  $('zi').onclick = () => dolly(0.7); $('zo').onclick = () => dolly(1 / 0.7);
  $('north').onclick = () => flyTo(controls.target, undefined, undefined, 0);
  $('top2').onclick = () => flyTo(controls.target, undefined, 0.02, 0);
  addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    const k = e.key, st = getView().radius * 0.06;
    if (k === 'ArrowUp') e.shiftKey ? rotate(0, 0.08) : pan(0, st);
    else if (k === 'ArrowDown') e.shiftKey ? rotate(0, -0.08) : pan(0, -st);
    else if (k === 'ArrowLeft') pan(-st, 0); else if (k === 'ArrowRight') pan(st, 0);
    else if (k === '+' || k === '=') dolly(0.85); else if (k === '-' || k === '_') dolly(1 / 0.85);
    else if (k === 'q' || k === 'Q') rotate(0.1, 0); else if (k === 'e' || k === 'E') rotate(-0.1, 0);
    else if (k === 'r' || k === 'R') flyTo(controls.target, undefined, undefined, 0);
    else if (k === 't' || k === 'T') flyTo(controls.target, undefined, 0.02, 0);
    else return;
    e.preventDefault();
  });

  // ------------------------------------------------------------------ recherche d'adresse (commune)
  if (MODE === 'commune') {
    const norm = s => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
    const addrIdx = DATA.addresses.map(a => ({a, k: norm(a[0])}));
    const q = $('q'), sugg = $('sugg');
    let hits = [], cur = -1;
    function showSugg() {
      const t = norm(q.value.trim());
      if (t.length < 2) { sugg.style.display = 'none'; hits = []; return; }
      const words = t.split(/\s+/);
      hits = addrIdx.filter(x => words.every(w => x.k.includes(w))).slice(0, 8);
      sugg.innerHTML = hits.map((h, i) => `<div data-i="${i}">${esc(h.a[0])}</div>`).join('') || '<div style="color:var(--mute)">Aucune adresse</div>';
      sugg.style.display = 'block'; cur = -1;
    }
    function goAddr(h) {
      const [label, x, y, mi] = h.a; sugg.style.display = 'none'; q.value = label;
      if (mi >= 0) select(mi, -1, true); else flyTo(new THREE.Vector3(x, 3, -y), 90, 0.9);
    }
    q.addEventListener('input', showSugg);
    q.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') cur = Math.min(cur + 1, hits.length - 1);
      else if (e.key === 'ArrowUp') cur = Math.max(cur - 1, 0);
      else if (e.key === 'Enter' && hits.length) { goAddr(hits[Math.max(cur, 0)]); return; }
      else if (e.key === 'Escape') { sugg.style.display = 'none'; return; } else return;
      [...sugg.children].forEach((c, i) => c.classList.toggle('on', i === cur)); e.preventDefault();
    });
    sugg.addEventListener('click', e => { const d = e.target.closest('[data-i]'); if (d) goAddr(hits[+d.dataset.i]); });
  }

  // ------------------------------------------------------------------ étiquettes : rues, numéros, pans
  const streetEls = (DATA.streets || []).map(s => {
    const el = document.createElement('div'); el.className = 'lb st'; el.textContent = s[0]; el.style.display = 'none'; layer.appendChild(el); return {el, s, w: 0};
  }).sort((a, b) => a.s[5] - b.s[5]);
  const numEls = B.map((b, i) => b.n ? {b, i} : null).filter(Boolean).map(o => {
    const el = document.createElement('div'); el.className = 'lb no'; el.textContent = o.b.n; el.style.display = 'none'; layer.appendChild(el); return {el, b: o.b, i: o.i, w: 0};
  });
  requestAnimationFrame(() => [streetEls, numEls].forEach(arr => arr.forEach(o => { o.el.style.display = 'block'; o.w = o.el.offsetWidth; o.el.style.display = 'none'; })));
  const v = new THREE.Vector3(), p1 = {x: 0, y: 0}, p2 = {x: 0, y: 0};
  function toScreen(x, y, z, out) {
    v.set(x, y, z).project(camera);
    if (v.z > 1 || v.z < -1) return false;
    out.x = (v.x * 0.5 + 0.5) * innerWidth; out.y = (-v.y * 0.5 + 0.5) * innerHeight; return true;
  }
  const hit2 = (a, b) => !(a.x2 < b.x1 || a.x1 > b.x2 || a.y2 < b.y1 || a.y1 > b.y2);
  function updateLabels() {
    const rects = [], free = r => { for (const o of rects) if (hit2(r, o)) return false; return true; };
    const dist = camera.position.distanceTo(controls.target);
    const showSt = $('streets').checked, showNo = $('nums').checked;
    const sr = $('side').getBoundingClientRect(); rects.push({x1: sr.left, y1: sr.top, x2: sr.right, y2: sr.bottom});
    if (info.style.display !== 'none') { const ir = info.getBoundingClientRect(); rects.push({x1: ir.left, y1: ir.top, x2: ir.right, y2: ir.bottom}); }
    panEls.forEach(({el, rec}) => {      // pastilles de pans : toujours affichées
      if (toScreen(rec.cx, rec.cz + 0.4, -rec.cy, p1)) { el.style.display = 'block'; el.style.transform = `translate(${(p1.x - 10).toFixed(1)}px,${(p1.y - 10).toFixed(1)}px)`; rects.push({x1: p1.x - 12, x2: p1.x + 12, y1: p1.y - 12, y2: p1.y + 12}); }
      else el.style.display = 'none';
    });
    for (const o of numEls) {
      const hide = () => { if (o.on) { o.el.style.display = 'none'; o.on = false; } };
      if (!showNo || dist > 420 || o.i === selB || !toScreen(o.b.cx, (o.b.h || 6) + 1, -o.b.cy, p1)) { hide(); continue; }
      const r = {x1: p1.x - o.w / 2, x2: p1.x + o.w / 2, y1: p1.y - 8, y2: p1.y + 8};
      if (p1.x < 0 || p1.y < 0 || p1.x > innerWidth || p1.y > innerHeight || !free(r)) { hide(); continue; }
      rects.push(r); o.el.style.display = 'block'; o.on = true;
      o.el.style.transform = `translate(${(p1.x - o.w / 2).toFixed(1)}px,${(p1.y - 8).toFixed(1)}px)`;
    }
    const maxSt = dist > 2600 ? 40 : 400; let n = 0;
    for (const o of streetEls) {
      const [, x, y, dx, dy] = o.s;
      let ok = showSt && n < maxSt && o.w && toScreen(x, 1.5, -y, p1) && toScreen(x + dx, 1.5, -(y + dy), p2), r;
      if (ok) {
        let ang = Math.atan2(p2.y - p1.y, p2.x - p1.x);
        if (ang > Math.PI / 2) ang -= Math.PI; else if (ang < -Math.PI / 2) ang += Math.PI;
        const c = Math.abs(Math.cos(ang)), s = Math.abs(Math.sin(ang));
        const hw = (o.w * c + 12 * s) / 2 + 3, hh = (o.w * s + 12 * c) / 2 + 3;
        r = {x1: p1.x - hw, x2: p1.x + hw, y1: p1.y - hh, y2: p1.y + hh};
        ok = p1.x > -50 && p1.y > -20 && p1.x < innerWidth + 50 && p1.y < innerHeight + 20 && free(r);
        if (ok) o.ang = ang;
      }
      if (!ok) { if (o.on) { o.el.style.display = 'none'; o.on = false; } continue; }
      rects.push(r); n++; o.el.style.display = 'block'; o.on = true;
      o.el.style.transform = `translate(${p1.x.toFixed(1)}px,${p1.y.toFixed(1)}px) rotate(${o.ang.toFixed(3)}rad) translate(${(-o.w / 2).toFixed(1)}px,-6px)`;
    }
    $('needle').style.transform = `rotate(${getView().theta}rad)`;
  }

  // ------------------------------------------------------------------ branchements
  ['min', 'others'].forEach(id => $(id) && $(id).addEventListener('input', applyFilters));
  $('metric').addEventListener('input', e => setMetric(e.target.value));
  $('photo').addEventListener('input', e => { ground.visible = e.target.checked; });
  function resize() { renderer.setSize(innerWidth, innerHeight, false); camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); }
  addEventListener('resize', resize); resize();

  // cadrage initial
  const home = {};
  (function frame() {
    const bb = new THREE.Box3();
    const target = MODE === 'address' ? [opts.focus ?? analysed[0]] : analysed;
    target.forEach(bi => { if (bi === undefined) return; B[bi].r.forEach(p => bb.expandByPoint(new THREE.Vector3(p[0], 0, -p[1]))); });
    if (bb.isEmpty()) bb.set(new THREE.Vector3(-100, 0, -100), new THREE.Vector3(100, 0, 100));
    const c = bb.getCenter(new THREE.Vector3()), sz = bb.getSize(new THREE.Vector3());
    home.target = c; home.dist = MODE === 'address' ? Math.max(sz.x, sz.z) * 1.8 + 45 : Math.max(sz.x, sz.z) * 1.1 + 250;
    controls.target.copy(c);
    camera.position.set(c.x, home.dist * 0.7, c.z + home.dist * 0.7); camera.lookAt(c);
  })();
  setDrag(false);
  setMetric('yield');
  if (MODE === 'address' && (opts.focus ?? analysed[0]) !== undefined) select(opts.focus ?? analysed[0], -1, false);
  (function loop() { stepAnim(); controls.update(); renderer.render(scene, camera); updateLabels(); requestAnimationFrame(loop); })();
  return {select, flyTo, B, roof, wall, panList, camera, controls};
}
