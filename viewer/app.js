/* Seat 4 · viewer/app.js — the run inspector.
 *
 * Pillar: evidence a judge can check. Reads ONE embedded JSON payload, written by
 * samanvay.report.dashboard.build_viewer into the #samanvay-run script tag, and
 * renders every number from it. Nothing here is hardcoded: with no payload the page
 * says "no run loaded" instead of showing a plausible figure.
 *
 * Why embedded and not fetched: fetch() and XHR are blocked by the browser on
 * file:// URLs, which is exactly how this page has to open on an air-gapped demo
 * machine. The alternative http path — serve the tree and load ?run=<dir> — is not
 * implemented; it would need a web server that may not exist on the day.
 *
 * Pan/zoom is hand-written on plain canvas because OpenSeadragon is not vendored in
 * this repo and must not be downloaded. If the team ever vendors it, replace the two
 * draw() calls at the OSD HOOK marker with two OSD viewers plus an addHandler("pan")
 * mirror; the view {px, py, z} state below is already the sync contract.
 *
 * ponytail: the two panes sync on *fractional* image position, not on the recovered
 * transform, so source and reference track each other loosely rather than pixel-for-
 * pixel. Upgrade path: map pane B's view through transform.params when it is present.
 */
(function () {
  "use strict";

  var D = null;
  try { D = JSON.parse(document.getElementById("samanvay-run").textContent); } catch (e) { D = null; }

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }
  function known(v) { return v !== null && v !== undefined; }
  function num(v, digits) {
    if (!known(v) || typeof v !== "number") { return null; }
    return Math.abs(v) >= 1000 ? v.toFixed(1) : v.toFixed(digits === undefined ? 3 : digits);
  }

  // ---------------------------------------------------------------- no payload
  if (!D) {
    $("run-name").textContent = "no run loaded";
    $("run-sub").textContent = "";
    $("banners").appendChild(el("div", "err",
      "This is the viewer shell. It carries no metrics of its own. Generate a page for a run:  " +
      "python -c \"from samanvay.report.dashboard import build_viewer; build_viewer('runs/demo_01')\"" +
      "  then open that run's viewer.html."));
    $("detail").textContent = "no run loaded";
    return;
  }

  var M = D.metrics || {};
  var matches = D.matches || [];
  var images = D.images || {};
  var notes = D.image_notes || {};

  // ---------------------------------------------------------------- sidebar
  $("run-name").textContent = D.run;
  $("run-sub").textContent = "generated " + (D.generated_utc || "unknown") +
    (D.report ? "" : " · no report.html beside this run");

  if (D.metrics_error) {
    $("banners").appendChild(el("div", "err", "no metrics: " + D.metrics_error));
  }
  if (M.rmse_trustworthy === false) {
    $("banners").appendChild(el("div", "warn", "rmse not trustworthy · " +
      (M.rmse_warning || "rmse_trustworthy is false and no warning text was recorded")));
  }
  if (D.matches_note) { $("banners").appendChild(el("div", "note", D.matches_note)); }
  Object.keys(notes).forEach(function (k) {
    $("banners").appendChild(el("div", "note", k + " imagery not embedded — " + notes[k]));
  });

  // check_rmse_px leads: it is measured on tie-points the fit never saw. rmse_px stays,
  // labelled in-sample, because it is not accuracy and must not be read as accuracy.
  var CARDS = [
    ["check_rmse_px", "check rmse px (held out)", 3], ["rmse_px", "rmse px (in-sample)", 3],
    ["n_check", "check points", 0], ["n_control", "control points", 0],
    ["gt_rmse_px", "gt rmse px", 3], ["sdi", "sdi", 3],
    ["inlier_count", "inliers", 0], ["match_count", "matches", 0],
    ["coverage_pct", "coverage %", 1], ["dispersion_cv", "dispersion cv", 2],
    ["runtime_s", "runtime s", 2], ["model_type", "model", null]
  ];
  CARDS.forEach(function (c) {
    var v = M[c[0]];
    var text = (c[2] === null) ? (known(v) ? String(v) : null) : num(v, c[2]);
    var card = el("div", "card" + (text === null ? " none" : "") +
      (c[0] === "rmse_px" && M.rmse_trustworthy === false ? " warnflag" : ""));
    if (text === null) { card.title = c[0] + " not reported by this run"; }
    if (c[0] === "rmse_px" && M.rmse_trustworthy === false) { card.title = M.rmse_warning || ""; }
    card.appendChild(el("div", "v", text === null ? "—" : text));
    card.appendChild(el("div", "l", c[1]));
    $("cards").appendChild(card);
  });

  (function inlierBar() {
    // The plan's >85% bar. A miss is shown as a miss: hiding it is the one thing this
    // page exists not to do.
    var pass = M.inlier_ratio_pass;
    if (pass === undefined) { pass = null; }
    if (pass === null && known(M.inlier_ratio) && known(M.inlier_ratio_target)) {
      pass = M.inlier_ratio >= M.inlier_ratio_target;
    }
    var card = el("div", "card" + (pass === null ? " none" : (pass ? "" : " failflag")));
    card.style.gridColumn = "1 / -1";
    card.title = "inlier_ratio " + (known(M.inlier_ratio) ? M.inlier_ratio : "unknown") +
                 " vs plan target " + (known(M.inlier_ratio_target) ? M.inlier_ratio_target : "unknown");
    var shown = num(M.inlier_ratio, 4);
    card.appendChild(el("div", "v", (pass === null ? "—" : (pass ? "PASS" : "FAIL")) + "  " +
                        (shown === null ? "—" : shown)));
    card.appendChild(el("div", "l", "inlier ratio vs plan target " +
                        (known(M.inlier_ratio_target) ? M.inlier_ratio_target : "—")));
    $("cards").appendChild(card);
  }());

  if (known(M.check_status) && M.check_status !== "ok") {
    $("banners").appendChild(el("div", "warn", "no held-out rmse · check_status=" +
      M.check_status + " — every match was used as a control point, so rmse px is " +
      "in-sample and must not be quoted as accuracy."));
  }

  (function stages() {
    var s = M.stage_s, box = $("stages");
    if (!s || !Object.keys(s).length) { box.appendChild(el("div", "note", "stage_s not reported")); return; }
    var max = 0;
    Object.keys(s).forEach(function (k) { if (typeof s[k] === "number" && s[k] > max) { max = s[k]; } });
    Object.keys(s).forEach(function (k) {
      var r = el("div", "stage");
      r.appendChild(el("span", null, k));
      var t = el("div", "track"), f = el("div", "fill");
      f.style.width = (typeof s[k] === "number" && max > 0 ? 100 * s[k] / max : 0).toFixed(1) + "%";
      t.appendChild(f); r.appendChild(t);
      r.appendChild(el("span", "v", typeof s[k] === "number" ? s[k].toFixed(3) : "—"));
      box.appendChild(r);
    });
  }());

  var STATES = { populated: "s-populated", insufficient_texture: "s-insufficient_texture",
                 masked_invalid: "s-masked_invalid" };
  (function uniformity() {
    var counts = M.cell_counts, states = M.cell_states, n = M.grid_n, box = $("ugrid");
    // The grid is rows x cols. geometry.uniformity.grid_shape keeps cells near-square on a
    // non-square source, so 400x1200 at grid_n=4 is 4 rows by 12 columns and an n*n test
    // would drop the whole grid. grid_n is the fallback for runs written before
    // grid_rows/grid_cols existed.
    var rows = M.grid_rows ? M.grid_rows : n, cols = M.grid_cols ? M.grid_cols : n;
    if (!counts || (!n && !cols)) { box.appendChild(el("div", "note", "no uniformity grid reported")); return; }
    if (counts.length !== rows * cols) {
      box.appendChild(el("div", "note", "cell_counts has " + counts.length + " cells, grid is " +
                     rows + "x" + cols + " which needs " + (rows * cols)));
      return;
    }
    var max = 1, i;
    for (i = 0; i < counts.length; i++) { if (typeof counts[i] === "number" && counts[i] > max) { max = counts[i]; } }
    var g = el("div", "ugrid");
    g.style.gridTemplateColumns = "repeat(" + cols + ",1fr)";
    for (i = 0; i < rows * cols; i++) {
      var st = (states && states[i]) ? String(states[i]) : "unknown";
      var c = el("div", "cell " + (STATES[st] || "s-unknown"));
      c.title = "cell " + i + " (col " + (i % cols) + ", row " + Math.floor(i / cols) + ") · " + st +
                " · count " + (known(counts[i]) ? counts[i] : "unknown");
      if (st === "populated") {
        c.textContent = known(counts[i]) ? counts[i] : "?";
        var f = typeof counts[i] === "number" ? Math.max(0.12, counts[i] / max) : 0.12;
        c.style.background = "rgba(0,188,212," + (0.10 + 0.72 * f).toFixed(3) + ")";
        c.style.color = f > 0.55 ? "#04222a" : "#f5f6f9";
      }
      g.appendChild(c);
    }
    box.appendChild(g);
    var leg = el("div", "legend");
    [["s-populated", "populated (count, shade = density)"],
     ["s-insufficient_texture", "insufficient texture"],
     ["s-masked_invalid", "masked / invalid"],
     ["s-unknown", "state not reported"]].forEach(function (p) {
      var s = el("span"), i2 = document.createElement("i");
      i2.className = p[0]; s.appendChild(i2); s.appendChild(document.createTextNode(p[1]));
      leg.appendChild(s);
    });
    box.appendChild(leg);
    box.appendChild(el("div", "note", "grid " + rows + " rows x " + cols + " cols · " +
                   (known(M.sdi_definition) ? M.sdi_definition : "sdi_definition not reported")));
  }());

  (function prov() {
    var p = D.provenance || {};
    var lines = [["run", D.run_path], ["run at", p.timestamp_utc],
                 ["git", p.git_sha ? String(p.git_sha).slice(0, 10) + (p.git_dirty ? " (dirty)" : "") : null],
                 ["illumination", M.illum_mode], ["phase congruency", M.pc_status],
                 ["canonicalised", known(M.canonicalised) ? String(M.canonicalised) : null],
                 ["match arm", M.match_method_resolved], ["arm chosen because", M.match_method_reason],
                 ["verify init", M.verify_init_source], ["mask fill", M.mask_fill],
                 ["clahe applied", known(M.clahe_applied) ? String(M.clahe_applied) : null],
                 ["seed applied", known(M.seed_applied) ? String(M.seed_applied) : null],
                 ["seed reason", M.seed_reason],
                 ["tps", known(M.tps_status) ? M.tps_status : null],
                 ["tps check rmse before/after", (known(M.tps_check_rmse_before_px) ||
                    known(M.tps_check_rmse_after_px))
                    ? (num(M.tps_check_rmse_before_px, 4) || "—") + " -> " +
                      (num(M.tps_check_rmse_after_px, 4) || "—")
                    : null]];
    lines.forEach(function (pair) {
      $("prov").appendChild(el("div", null, pair[0] + ": " + (known(pair[1]) ? pair[1] : "not reported")));
    });
    if (D.report) {
      var a = document.createElement("a");
      a.href = D.report; a.textContent = "open report.html";
      $("prov").appendChild(a);
    }
  }());

  $("bar-left").textContent = "drag to pan · wheel to zoom · panes are synced";
  $("bar-right").textContent = "matches: " + matches.length +
    (known(M.inlier_count) ? " · inliers: " + M.inlier_count : "");

  // ---------------------------------------------------------------- imagery
  var loaded = {};
  Object.keys(images).forEach(function (k) {
    var im = new Image();
    im.onload = function () { loaded[k] = im; drawAll(); };
    im.src = images[k].src;
  });

  function dims(key) {                       // full-resolution pixel extent of a raster
    var meta = images[key];
    if (meta && meta.full_w && meta.full_h) { return { w: meta.full_w, h: meta.full_h }; }
    return null;
  }
  function extent(key, xk, yk) {             // fall back to the tie-points' own bounding box
    var d = dims(key);
    if (d) { return d; }
    var maxx = 1, maxy = 1;
    matches.forEach(function (m) {
      if (typeof m[xk] === "number" && m[xk] > maxx) { maxx = m[xk]; }
      if (typeof m[yk] === "number" && m[yk] > maxy) { maxy = m[yk]; }
    });
    return { w: maxx * 1.05, h: maxy * 1.05, guessed: true };
  }

  var extA = extent("source", "src_x", "src_y");
  var extB = extent("reference", "ref_x", "ref_y");
  $("note-a").textContent = images.source ? "" : ("source imagery not embedded" +
    (extA.guessed ? " — points placed on their own bounding box" : ""));
  $("note-b").textContent = (images.reference || images.registered) ? "" :
    ("reference imagery not embedded" + (extB.guessed ? " — points placed on their own bounding box" : ""));

  // ---------------------------------------------------------------- view state
  var view = { px: 0.5, py: 0.5, z: 1 };     // normalised point at canvas centre, zoom over fit
  var mode = "swipe", swipe = 0.5, selected = -1;
  var hits = { A: [], B: [] };

  var MODES = [["reference", "reference"], ["registered", "registered"],
               ["swipe", "swipe"], ["checker", "checkerboard"]];
  MODES.forEach(function (m) {
    var b = el("button", m[0] === mode ? "on" : null, m[1]);
    b.onclick = function () {
      mode = m[0];
      Array.prototype.forEach.call($("modes").children, function (c) { c.className = ""; });
      b.className = "on";
      drawAll();
    };
    $("modes").appendChild(b);
  });
  $("swipe").oninput = function () { swipe = this.value / 100; if (mode === "swipe") { drawAll(); } };
  $("show-matches").onchange = drawAll;
  $("only-inliers").onchange = drawAll;

  function fit(canvas, ext) {                // geometry of the drawn image in CSS pixels
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    var base = Math.min(cw / ext.w, ch / ext.h);
    var w = ext.w * base * view.z, h = ext.h * base * view.z;
    return { x: cw / 2 - view.px * w, y: ch / 2 - view.py * h, w: w, h: h, cw: cw, ch: ch };
  }

  function drawPane(canvas, which) {
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    if (!cw || !ch) { return; }
    canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#0f1219"; ctx.fillRect(0, 0, cw, ch);
    ctx.imageSmoothingEnabled = view.z < 4;

    var ext = which === "A" ? extA : extB;
    var g = fit(canvas, ext);

    /* OSD HOOK — replace this block with an OpenSeadragon viewer if the team ever
       vendors it locally; keep view {px,py,z} as the shared sync state. */
    if (which === "A") {
      if (loaded.source) { ctx.drawImage(loaded.source, g.x, g.y, g.w, g.h); }
    } else {
      var ref = loaded.reference, reg = loaded.registered;
      var base = (mode === "registered" && reg) ? reg : (ref || reg);
      if (base) { ctx.drawImage(base, g.x, g.y, g.w, g.h); }
      if (reg && ref && (mode === "swipe" || mode === "checker")) {
        ctx.save();
        ctx.beginPath();
        if (mode === "swipe") {
          ctx.rect(cw * swipe, 0, cw - cw * swipe, ch);
        } else {
          var tiles = 8, tw = g.w / tiles, th = g.h / tiles;
          for (var r = 0; r < tiles; r++) {
            for (var c = 0; c < tiles; c++) {
              if ((r + c) % 2 === 1) { ctx.rect(g.x + c * tw, g.y + r * th, tw, th); }
            }
          }
        }
        ctx.clip();
        ctx.drawImage(reg, g.x, g.y, g.w, g.h);
        ctx.restore();
        if (mode === "swipe") {
          ctx.strokeStyle = "#00bcd4"; ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.moveTo(cw * swipe, 0); ctx.lineTo(cw * swipe, ch); ctx.stroke();
        }
      }
    }

    hits[which] = [];
    if (!$("show-matches").checked) { return; }
    var onlyIn = $("only-inliers").checked;
    var xk = which === "A" ? "src_x" : "ref_x", yk = which === "A" ? "src_y" : "ref_y";
    matches.forEach(function (m, i) {
      var inlier = !!m.is_inlier;
      if (onlyIn && !inlier) { return; }
      if (typeof m[xk] !== "number" || typeof m[yk] !== "number") { return; }
      var x = g.x + (m[xk] / ext.w) * g.w, y = g.y + (m[yk] / ext.h) * g.h;
      if (x < -20 || y < -20 || x > cw + 20 || y > ch + 20) { return; }
      hits[which].push({ i: i, x: x, y: y });
      ctx.beginPath();
      ctx.arc(x, y, i === selected ? 6 : 3.2, 0, 6.284);
      ctx.fillStyle = inlier ? "rgba(0,188,212,.85)" : "rgba(255,83,112,.85)";
      ctx.fill();
      if (i === selected) { ctx.strokeStyle = "#f5f6f9"; ctx.lineWidth = 1.6; ctx.stroke(); }
    });
  }

  function drawAll() { drawPane($("cvA"), "A"); drawPane($("cvB"), "B"); }

  function showMatch(i) {
    selected = i;
    var box = $("detail"); box.innerHTML = "";
    if (i < 0 || !matches[i]) { box.textContent = "click a match point"; drawAll(); return; }
    var m = matches[i];
    [["id", known(m.id) ? m.id : i],
     ["state", m.is_inlier ? "inlier" : "outlier"],
     ["src x,y", num(m.src_x, 2) + ", " + num(m.src_y, 2)],
     ["ref x,y", num(m.ref_x, 2) + ", " + num(m.ref_y, 2)],
     ["residual px", num(m.residual_px, 4)],
     ["sigma px", num(m.sigma_px, 4)],
     ["score", num(m.score, 4)],
     ["grid cell", known(m.grid_cell) ? m.grid_cell : null]].forEach(function (p) {
      var line = el("div");
      line.appendChild(el("b", null, p[0] + ": "));
      line.appendChild(document.createTextNode(known(p[1]) && p[1] !== "null, null" ? String(p[1]) : "unknown"));
      box.appendChild(line);
    });
    drawAll();
  }

  // ---------------------------------------------------------------- interaction
  ["A", "B"].forEach(function (which) {
    var canvas = $(which === "A" ? "cvA" : "cvB");
    var drag = null, moved = 0;
    canvas.addEventListener("pointerdown", function (ev) {
      canvas.setPointerCapture(ev.pointerId);
      drag = { x: ev.clientX, y: ev.clientY }; moved = 0;
    });
    canvas.addEventListener("pointermove", function (ev) {
      if (!drag) { return; }
      var g = fit(canvas, which === "A" ? extA : extB);
      var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      moved += Math.abs(dx) + Math.abs(dy);
      drag = { x: ev.clientX, y: ev.clientY };
      view.px -= dx / g.w; view.py -= dy / g.h;
      drawAll();
    });
    canvas.addEventListener("pointerup", function (ev) {
      drag = null;
      if (moved > 4) { return; }
      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top, best = -1, bd = 12 * 12;
      hits[which].forEach(function (h) {
        var d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
        if (d < bd) { bd = d; best = h.i; }
      });
      showMatch(best);
    });
    canvas.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var g = fit(canvas, which === "A" ? extA : extB);
      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      var u = (mx - g.x) / g.w, v = (my - g.y) / g.h;
      var k = Math.exp(-ev.deltaY * 0.0015);
      view.z = Math.min(80, Math.max(0.2, view.z * k));
      var g2 = fit(canvas, which === "A" ? extA : extB);
      view.px = (g2.cw / 2 - mx) / g2.w + u;
      view.py = (g2.ch / 2 - my) / g2.h + v;
      drawAll();
    }, { passive: false });
  });

  window.addEventListener("resize", drawAll);
  $("label-a").textContent = "source" + (images.source ? "" : " (no imagery)");
  $("label-b").textContent = "reference / registered";
  drawAll();
}());
