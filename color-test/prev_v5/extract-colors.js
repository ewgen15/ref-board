/* Витягування палітри з референсів. Чистий JS, без залежностей (v4: «плоскі пікселі + мода»).
 * Логіка повторює extract_palettes.py (еталон).
 *
 *   extractPalette(imageData, opts) -> [{hex, share, role[, removed]}, ...]
 *   imageData: {data: Uint8ClampedArray RGBA, width, height}
 *   opts: {rect: {x,y,w,h} у відсотках, margins: true, center: false, edgeOnly: false,
 *          removeBackground: true, detailSide: 800, hiRes: false}
 *
 * Дві копії:
 *   мала (200 px): кластери k-means, частки площі, фон/поверхня (видалення, якщо фон > 50 %), решта;
 *   велика (detailSide, або повний розмір при hiRes): hex = МОДА точного RGB серед плоских пікселів
 *   (8 сусідів у 3x3 з max Δканалу <= 4), найближчих до центру кластера; окремий прохід акценту
 *   (плоскі насичені кольори, далекі за відтінком/контрастом від фону й поверхні, >= 2 плями,
 *   оцінка = насиченість * плоскість * повторюваність, до 3 акцентів).
 *
 * Браузер: <script src="extract-colors.js"></script>, далі ColorExtract.extractPalette(...)
 *          ColorExtract.imageDataFromSource(fileOrBlob, 800) -> Promise<imageData>
 *          (maxSide = null -> повний розмір, для hiRes)
 * Node:    const CE = require('./extract-colors.js');
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ColorExtract = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var COARSE_SIDE = 200, DETAIL_SIDE = 800, K = 8, ITERS = 10, MERGE_DE = 8.0, MIN_SHARE_OUT = 0.001;
  var SURFACE_MIN_SHARE = 0.05;
  var TEXT_MIN_SHARE = 0.002, TEXT_MAX_SHARE = 0.30, TEXT_NEUTRAL_CHROMA = 30.0, TEXT_MIN_DE_BG = 20.0;
  var MARGIN_WHITE = 245, MARGIN_MAX_FRAC = 0.15, EDGE_BAND = 0.03;
  var REMOVE_BG_SHARE = 0.5, REMOVE_DE = 8.0, REMOVE_SURFACE_SHARE = 0.5, MAX_PASSES = 2;
  var FLAT_TOL = 4, FLAT_MIN_PX = 8;
  var ACC_MIN_CHROMA = 25.0, ACC_NEUTRAL_BG = 8.0, ACC_HUE_FAR = 20.0, ACC_FAR_DE = 50.0;
  var ACC_GROUP_DE = 12.0, ACC_GROUPS_MAX = 40;
  var ACC_MIN_BLOB_PX = 12, ACC_MIN_BLOB_FRAC = 0.00002, ACC_MIN_BLOBS = 2, ACC_REP_CAP = 4, ACC_SAT_FULL = 60.0;
  var ACC_PICK_MAX = 3, ACC_PICK_RATIO = 0.4, ACC_PICK_DE = 12.0, ACC_DROP_DE = 12.0;
  var NB = 32768;

  var D65X = 0.95047, D65Z = 1.08883;
  var RAD2DEG = 180 / Math.PI;

  function srgbToLab(r, g, b) {
    function lin(c) {
      c = c / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    var R = lin(r), G = lin(g), B = lin(b);
    var X = (0.4124 * R + 0.3576 * G + 0.1805 * B) / D65X;
    var Y = (0.2126 * R + 0.7152 * G + 0.0722 * B);
    var Z = (0.0193 * R + 0.1192 * G + 0.9505 * B) / D65Z;
    function f(t) { return t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116; }
    var fx = f(X), fy = f(Y), fz = f(Z);
    return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
  }

  function relLuminance(r, g, b) {
    function lin(c) {
      c = c / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  }

  function contrastRatio(c1, c2) {
    var l1 = relLuminance(c1[0], c1[1], c1[2]), l2 = relLuminance(c2[0], c2[1], c2[2]);
    var hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  }

  function dist3(a, b) {
    var x = a[0] - b[0], y = a[1] - b[1], z = a[2] - b[2];
    return Math.sqrt(x * x + y * y + z * z);
  }

  function roundHalfUp(v) { return Math.floor(v + 0.5); }

  function hex2(v) {
    var h = Math.max(0, Math.min(255, roundHalfUp(v))).toString(16);
    return h.length < 2 ? '0' + h : h;
  }

  function hexOfRgb(rgb) { return '#' + hex2(rgb[0]) + hex2(rgb[1]) + hex2(rgb[2]); }
  function hexOfKey(k) { return '#' + hex2((k >> 16) & 255) + hex2((k >> 8) & 255) + hex2(k & 255); }
  function hexToRgb(h) { return [1, 3, 5].map(function (i) { return parseInt(h.slice(i, i + 2), 16); }); }
  function hexToLab(h) { var c = hexToRgb(h); return srgbToLab(c[0], c[1], c[2]); }

  // Прямокутник у відсотках -> піксельні межі (та сама формула, що в Python)
  function cropRect(src, rect) {
    var W = src.width, H = src.height;
    var x0 = Math.floor(W * rect.x / 100), y0 = Math.floor(H * rect.y / 100);
    var x1 = Math.floor(W * (rect.x + rect.w) / 100), y1 = Math.floor(H * (rect.y + rect.h) / 100);
    x1 = Math.max(x1, x0 + 1); y1 = Math.max(y1, y0 + 1);
    var cw = x1 - x0, ch = y1 - y0;
    var out = new Uint8ClampedArray(cw * ch * 4);
    for (var y = 0; y < ch; y++) {
      var srcOff = ((y0 + y) * W + x0) * 4;
      out.set(src.data.subarray(srcOff, srcOff + cw * 4), y * cw * 4);
    }
    return { data: out, width: cw, height: ch };
  }

  // Box-фільтр: середнє по блоках, округлення half-up через ціле ділення (як у Python).
  function downscale(src, maxSide) {
    var W = src.width, H = src.height;
    var s = Math.max(W, H);
    if (s <= maxSide) return src;
    var f = maxSide / s;
    var nw = Math.max(1, Math.floor(W * f + 0.5)), nh = Math.max(1, Math.floor(H * f + 0.5));
    var out = new Uint8ClampedArray(nw * nh * 4);
    for (var y = 0; y < nh; y++) {
      var y0 = Math.floor(y * H / nh), y1 = Math.max(y0 + 1, Math.floor((y + 1) * H / nh));
      for (var x = 0; x < nw; x++) {
        var x0 = Math.floor(x * W / nw), x1 = Math.max(x0 + 1, Math.floor((x + 1) * W / nw));
        var n = (y1 - y0) * (x1 - x0);
        var a0 = 0, a1 = 0, a2 = 0, a3 = 0;
        for (var yy = y0; yy < y1; yy++)
          for (var xx = x0; xx < x1; xx++) {
            var o = (yy * W + xx) * 4;
            a0 += src.data[o]; a1 += src.data[o + 1]; a2 += src.data[o + 2]; a3 += src.data[o + 3];
          }
        var d = (y * nw + x) * 4;
        out[d] = Math.floor((2 * a0 + n) / (2 * n));
        out[d + 1] = Math.floor((2 * a1 + n) / (2 * n));
        out[d + 2] = Math.floor((2 * a2 + n) / (2 * n));
        out[d + 3] = Math.floor((2 * a3 + n) / (2 * n));
      }
    }
    return { data: out, width: nw, height: nh };
  }

  // Біла смуга полів: білі пікселі, зв'язані з межею; якщо їх >= 15 % площі — це фон, не поля.
  function marginMask(data, W, H, valid) {
    var N = W * H;
    var seen = new Uint8Array(N), cand = new Uint8Array(N), q = new Int32Array(N);
    for (var i = 0; i < N; i++) {
      var o = i * 4;
      cand[i] = valid[i] && Math.min(data[o], data[o + 1], data[o + 2]) >= MARGIN_WHITE ? 1 : 0;
    }
    var head = 0, tail = 0;
    for (var y = 0; y < H; y++)
      for (var x = 0; x < W; x++) {
        if ((y === 0 || y === H - 1 || x === 0 || x === W - 1) && cand[y * W + x]) {
          seen[y * W + x] = 1; q[tail++] = y * W + x;
        }
      }
    while (head < tail) {
      var p = q[head++], py = Math.floor(p / W), px = p - py * W;
      var nb = [[py - 1, px], [py + 1, px], [py, px - 1], [py, px + 1]];
      for (var k = 0; k < 4; k++) {
        var ny = nb[k][0], nx = nb[k][1];
        if (ny >= 0 && ny < H && nx >= 0 && nx < W) {
          var ni = ny * W + nx;
          if (cand[ni] && !seen[ni]) { seen[ni] = 1; q[tail++] = ni; }
        }
      }
    }
    if (tail >= MARGIN_MAX_FRAC * N) return new Uint8Array(N);
    return seen;
  }

  // Плоский піксель: усі 8 сусідів у 3x3 відрізняються max Δканалу <= FLAT_TOL; краї кадру — не плоскі.
  function flatMask(data, W, H) {
    var flat = new Uint8Array(W * H);
    if (W < 3 || H < 3) return flat;
    for (var y = 1; y < H - 1; y++)
      for (var x = 1; x < W - 1; x++) {
        var o = (y * W + x) * 4, ok = 1;
        for (var dy = -1; dy <= 1 && ok; dy++)
          for (var dx = -1; dx <= 1 && ok; dx++) {
            if (dy === 0 && dx === 0) continue;
            var q = ((y + dy) * W + (x + dx)) * 4;
            var m = Math.max(Math.abs(data[o] - data[q]), Math.abs(data[o + 1] - data[q + 1]),
                             Math.abs(data[o + 2] - data[q + 2]));
            if (m > FLAT_TOL) ok = 0;
          }
        flat[y * W + x] = ok;
      }
    return flat;
  }

  // Пікселі малої копії -> кошики 5 біт: {lab, w, means} або null
  function buildBins(src, opts) {
    var W = src.width, H = src.height, data = src.data, N = W * H;
    var valid = new Uint8Array(N);
    var i;
    for (i = 0; i < N; i++) valid[i] = data[i * 4 + 3] >= 128 ? 1 : 0;
    if (opts.margins !== false) {
      var mm = marginMask(data, W, H, valid);
      for (i = 0; i < N; i++) if (mm[i]) valid[i] = 0;
    }
    var weight = new Float64Array(N).fill(1);
    if (opts.center) {
      var cx = (W - 1) / 2, cy = (H - 1) / 2;
      var dmax = Math.max(Math.sqrt((W / 2) * (W / 2) + (H / 2) * (H / 2)), 1e-9);
      for (var y = 0; y < H; y++)
        for (var x = 0; x < W; x++) {
          var d = Math.sqrt((x - cx) * (x - cx) + (y - cy) * (y - cy));
          weight[y * W + x] = 1 + (1 - d / dmax);
        }
    }
    var cnt = new Float64Array(NB), sr = new Float64Array(NB), sg = new Float64Array(NB), sb = new Float64Array(NB);
    var bandw = new Float64Array(NB);
    var idxOf = new Int32Array(N);
    // смуга по краю кадру (периметр): товщина EDGE_BAND по кожній стороні (як у Python)
    var bh = Math.max(1, roundHalfUp(EDGE_BAND * H)), bw = Math.max(1, roundHalfUp(EDGE_BAND * W));
    for (i = 0; i < N; i++) {
      var o = i * 4;
      idxOf[i] = ((data[o] >> 3) << 10) | ((data[o + 1] >> 3) << 5) | (data[o + 2] >> 3);
      if (!valid[i]) continue;
      var wv = weight[i], b = idxOf[i];
      cnt[b] += wv; sr[b] += data[o] * wv; sg[b] += data[o + 1] * wv; sb[b] += data[o + 2] * wv;
      var px = i % W, py = (i - px) / W;
      if (px < bw || px >= W - bw || py < bh || py >= H - bh) bandw[b] += wv;
    }
    if (opts.edgeOnly) {
      var hasInner = new Uint8Array(NB);
      for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) {
          var pi = y * W + x;
          if (valid[pi] && x >= bw && x < W - bw && y >= bh && y < H - bh) hasInner[idxOf[pi]] = 1;
        }
      for (var bi = 0; bi < NB; bi++) if (!hasInner[bi]) cnt[bi] = 0;
    }
    var lab = [], w = [], means = [], bwOut = [];
    for (var bk = 0; bk < NB; bk++) {
      if (cnt[bk] > 0) {
        var mr = [sr[bk] / cnt[bk], sg[bk] / cnt[bk], sb[bk] / cnt[bk]];
        means.push(mr);
        lab.push(srgbToLab(mr[0], mr[1], mr[2]));
        w.push(cnt[bk]);
        bwOut.push(bandw[bk]);
      }
    }
    if (!w.length) return null;
    return { lab: lab, w: w, means: means, bandw: bwOut };
  }

  // k-means (детермінований) + злиття ΔE < MERGE_DE. Кластери з абсолютними вагами.
  // bandw: вага смуги по периметру для кошика (необов'язково); сумується в кластері.
  function clusterBins(lab, w, means, bandw) {
    var M = w.length, k = Math.min(K, M);
    if (!bandw) bandw = w.map(function () { return 0; });
    var seedIdx = 0;
    for (var i = 1; i < M; i++) if (w[i] > w[seedIdx]) seedIdx = i;
    var C = [lab[seedIdx].slice()];
    var d2 = lab.map(function (l) { var dd = dist3(l, C[0]); return dd * dd; });
    while (C.length < k) {
      var best = -1, bs = -Infinity;
      for (i = 0; i < M; i++) { var sc = w[i] * d2[i]; if (sc > bs) { bs = sc; best = i; } }
      if (bs <= 0) break;
      C.push(lab[best].slice());
      for (i = 0; i < M; i++) { var dd2 = dist3(lab[i], C[C.length - 1]); d2[i] = Math.min(d2[i], dd2 * dd2); }
    }
    var asg = new Int32Array(M);
    function assign() {
      for (var m = 0; m < M; m++) {
        var bc = 0, bd = Infinity;
        for (var c = 0; c < C.length; c++) {
          var dd = dist3(lab[m], C[c]); var q2 = dd * dd;
          if (q2 < bd) { bd = q2; bc = c; }
        }
        asg[m] = bc;
      }
    }
    for (var it = 0; it < ITERS; it++) {
      assign();
      for (var c2 = 0; c2 < C.length; c2++) {
        var sx = 0, sy = 0, sz = 0, sw = 0;
        for (var m2 = 0; m2 < M; m2++) if (asg[m2] === c2) {
          var w2 = w[m2];
          sx += lab[m2][0] * w2; sy += lab[m2][1] * w2; sz += lab[m2][2] * w2; sw += w2;
        }
        if (sw > 0) C[c2] = [sx / sw, sy / sw, sz / sw];
      }
    }
    assign();

    var clusters = [];
    for (var c3 = 0; c3 < C.length; c3++) {
      var acc = { lab: [0, 0, 0], rgb: [0, 0, 0], w: 0, bandw: 0 }, any = false;
      for (var m3 = 0; m3 < M; m3++) if (asg[m3] === c3) {
        any = true;
        var w3 = w[m3];
        for (var t = 0; t < 3; t++) { acc.lab[t] += lab[m3][t] * w3; acc.rgb[t] += means[m3][t] * w3; }
        acc.w += w3;
        acc.bandw += bandw[m3];
      }
      if (!any) continue;
      clusters.push({ lab: acc.lab.map(function (v) { return v / acc.w; }),
                      rgb: acc.rgb.map(function (v) { return v / acc.w; }), w: acc.w, bandw: acc.bandw });
    }

    while (clusters.length > 1) {
      var bI = -1, bJ = -1, bDe = Infinity;
      for (i = 0; i < clusters.length; i++)
        for (var j = i + 1; j < clusters.length; j++) {
          var de = dist3(clusters[i].lab, clusters[j].lab);
          if (de < bDe) { bDe = de; bI = i; bJ = j; }
        }
      if (bDe >= MERGE_DE) break;
      var A = clusters[bI], Bc = clusters[bJ], Wc = A.w + Bc.w;
      var merged = {
        lab: [0, 1, 2].map(function (t) { return (A.lab[t] * A.w + Bc.lab[t] * Bc.w) / Wc; }),
        rgb: [0, 1, 2].map(function (t) { return (A.rgb[t] * A.w + Bc.rgb[t] * Bc.w) / Wc; }),
        w: Wc,
        bandw: A.bandw + Bc.bandw
      };
      clusters = clusters.filter(function (_, t) { return t !== bI && t !== bJ; }).concat([merged]);
    }
    return clusters;
  }

  function prep(c) {
    c.hex = hexOfRgb(c.rgb);
    c.chroma = Math.sqrt(c.lab[1] * c.lab[1] + c.lab[2] * c.lab[2]);
  }

  // Частки відносно total, hex, chroma; відкидаємо шум; сортуємо за часткою (спадно)
  function finalize(clusters, total) {
    clusters.forEach(function (c) { c.share = c.w / total; prep(c); });
    clusters = clusters.filter(function (c) { return c.share >= MIN_SHARE_OUT; });
    clusters.sort(function (a, b) { return b.share - a.share; });
    return clusters;
  }

  // Кластеризація залишку; частки відносно суми залишку
  function residual(rl, rw, rm) {
    if (!rw.length) return [];
    var total = rw.reduce(function (s, v) { return s + v; }, 0);
    return finalize(clusterBins(rl, rw, rm), total);
  }

  // Поверхня: найближчий до фону кластер з часткою > 5 %; ставить роль і повертає його (або null)
  function pickSurface(rest, bg) {
    var cands = rest.filter(function (c) { return c.share > SURFACE_MIN_SHARE; });
    if (!cands.length) return null;
    var best = null, bv = Infinity;
    cands.forEach(function (c) { var v = dist3(c.lab, bg.lab); if (v < bv) { bv = v; best = c; } });
    best.role = 'surface';
    return best;
  }

  // textPrimary / textSecondary серед решти; решта -> other
  function assignTextRoles(rest, bg) {
    rest.forEach(function (c) { c.role = 'other'; });
    function deBg(c) { return dist3(c.lab, bg.lab); }
    function textOk(c) { return c.share >= TEXT_MIN_SHARE && c.share <= TEXT_MAX_SHARE && deBg(c) >= TEXT_MIN_DE_BG; }
    function neutralFirst(pool) {
      var aa = pool.filter(function (c) { return contrastRatio(c.rgb, bg.rgb) >= 4.5; });
      if (aa.length) pool = aa;
      var neutral = pool.filter(function (c) { return c.chroma <= TEXT_NEUTRAL_CHROMA; });
      return neutral.length ? neutral : pool;
    }
    function pickBy(arr, fn, better) {
      var best = null, bv = null;
      arr.forEach(function (c) { var v = fn(c); if (bv === null || better(v, bv)) { bv = v; best = c; } });
      return best;
    }
    var tp = null;
    var tc = neutralFirst(rest.filter(textOk));
    if (tc.length) {
      tp = pickBy(tc, function (c) { return contrastRatio(c.rgb, bg.rgb); }, function (a, b) { return a > b; });
      tp.role = 'textPrimary';
    }
    if (tp) {
      var lo = Math.min(bg.lab[0], tp.lab[0]), hi = Math.max(bg.lab[0], tp.lab[0]);
      var mid = (bg.lab[0] + tp.lab[0]) / 2;
      var sec = neutralFirst(rest.filter(function (c) {
        return c.role === 'other' && textOk(c) && c.lab[0] > lo && c.lab[0] < hi;
      }));
      if (sec.length) {
        pickBy(sec, function (c) { return Math.abs(c.lab[0] - mid); }, function (a, b) { return a < b; }).role = 'textSecondary';
      }
    }
  }

  // Мала копія: кластери, фон і поверхня (з видаленням, якщо фон > 50 %), решта.
  // Фон (v5) — кластер з найбільшою часткою смуги по периметру (при нічиїй — найбільший).
  function structure(src, opts) {
    var b = buildBins(src, opts);
    if (!b) return null;
    var lab = b.lab, w = b.w, means = b.means;
    var totalAll = w.reduce(function (s, v) { return s + v; }, 0);
    var clusters = finalize(clusterBins(lab, w, means, b.bandw), totalAll);
    if (!clusters.length) return null;
    var per = 0;
    for (var pi0 = 1; pi0 < clusters.length; pi0++) if (clusters[pi0].bandw > clusters[per].bandw) per = pi0;
    var bg = clusters.splice(per, 1)[0];
    clusters.unshift(bg);
    if (opts.removeBackground === false || bg.share <= REMOVE_BG_SHARE) {
      bg.role = 'background';
      var rest0 = clusters.slice(1);
      var surf0 = pickSurface(rest0, bg);
      if (surf0) rest0 = rest0.filter(function (c) { return c !== surf0; });
      return { bg: bg, surf: surf0, surfRemoved: false, rest: rest0, removal: false, totalAll: totalAll };
    }
    var keep = [];
    for (var i = 0; i < lab.length; i++) if (dist3(lab[i], bg.lab) > REMOVE_DE) keep.push(i);
    var rl = keep.map(function (j) { return lab[j]; }), rw = keep.map(function (j) { return w[j]; }),
        rm = keep.map(function (j) { return means[j]; });
    var rest = residual(rl, rw, rm);
    var surf = null, surfRemoved = false;
    if (rest.length && rest[0].share > REMOVE_SURFACE_SHARE) {
      surf = rest[0]; surfRemoved = true;
      var keep2 = [];
      for (i = 0; i < rl.length; i++) if (dist3(rl[i], surf.lab) > REMOVE_DE) keep2.push(i);
      rl = keep2.map(function (j) { return rl[j]; });
      rw = keep2.map(function (j) { return rw[j]; });
      rm = keep2.map(function (j) { return rm[j]; });
      rest = residual(rl, rw, rm);
    } else {
      surf = pickSurface(rest, bg);
      if (surf) rest = rest.filter(function (c) { return c !== surf; });
    }
    bg.role = 'background';
    return { bg: bg, surf: surf, surfRemoved: surfRemoved, rest: rest, removal: true, totalAll: totalAll };
  }

  // Далекий від ref: за відтінком (>= ACC_HUE_FAR) або за контрастом (ΔE >= ACC_FAR_DE).
  // Майже нейтральний ref: далекі всі кольори.
  function farFrom(lab, ref) {
    var rl = ref.lab;
    if (Math.hypot(rl[1], rl[2]) < ACC_NEUTRAL_BG) return true;
    var h = Math.atan2(lab[2], lab[1]) * RAD2DEG;
    var hr = Math.atan2(rl[2], rl[1]) * RAD2DEG;
    var d = Math.abs(h - hr) % 360;
    d = Math.min(d, 360 - d);
    return d >= ACC_HUE_FAR || dist3(lab, rl) >= ACC_FAR_DE;
  }

  // Пошук малого акценту на великій копії. fkeys відсортовані; fcnt — плоскі частоти; fall — усі частоти;
  // fIdx[i] — індекс у fkeys для плоского валідного пікселя i, інакше -1.
  function accentPass(fkeys, fcnt, fall, bg, surf, nValid, W, H, fIdx) {
    var M = fkeys.length;
    if (!M) return [];
    var flab = [], fch = [], i;
    for (i = 0; i < M; i++) {
      var k = fkeys[i];
      flab.push(srgbToLab((k >> 16) & 255, (k >> 8) & 255, k & 255));
      fch.push(Math.hypot(flab[i][1], flab[i][2]));
    }
    var ok = new Uint8Array(M);
    for (i = 0; i < M; i++) {
      ok[i] = (fch[i] >= ACC_MIN_CHROMA && farFrom(flab[i], bg) && (!surf || farFrom(flab[i], surf))) ? 1 : 0;
    }
    var order = [];
    for (i = 0; i < M; i++) order.push(i);
    order.sort(function (a, b) { return (fcnt[b] - fcnt[a]) || (a - b); });

    var groups = [];
    var gidF = new Int32Array(M).fill(-1);
    order.forEach(function (ii) {
      if (!ok[ii] || fcnt[ii] < 2) return;
      var j = -1;
      if (groups.length) {
        var bd = Infinity, bj = -1;
        for (var g = 0; g < groups.length; g++) {
          var dd = dist3(groups[g].lab, flab[ii]);
          if (dd < bd) { bd = dd; bj = g; }
        }
        if (bd <= ACC_GROUP_DE) j = bj;
      }
      if (j >= 0) {
        groups[j].fsum += fcnt[ii];
        groups[j].asum += fall[ii];
      } else {
        if (groups.length >= ACC_GROUPS_MAX) return;
        groups.push({ lab: flab[ii], hex: hexOfKey(fkeys[ii]), chroma: fch[ii], fsum: fcnt[ii], asum: fall[ii] });
        j = groups.length - 1;
      }
      gidF[ii] = j;
    });
    if (!groups.length) return [];

    var N = W * H;
    var Gimg = new Int32Array(N).fill(-1);
    for (i = 0; i < N; i++) if (fIdx[i] >= 0) Gimg[i] = gidF[fIdx[i]];

    var minb = Math.max(ACC_MIN_BLOB_PX, Math.floor(ACC_MIN_BLOB_FRAC * nValid + 0.5));
    var visited = new Uint8Array(N);
    var blobs = groups.map(function () { return []; });
    for (var p = 0; p < N; p++) {
      if (Gimg[p] < 0 || visited[p]) continue;
      var gg = Gimg[p];
      visited[p] = 1;
      var stack = [p], n = 0;
      while (stack.length) {
        var q = stack.pop();
        n++;
        var y = Math.floor(q / W), x = q - y * W;
        if (y > 0 && !visited[q - W] && Gimg[q - W] === gg) { visited[q - W] = 1; stack.push(q - W); }
        if (y < H - 1 && !visited[q + W] && Gimg[q + W] === gg) { visited[q + W] = 1; stack.push(q + W); }
        if (x > 0 && !visited[q - 1] && Gimg[q - 1] === gg) { visited[q - 1] = 1; stack.push(q - 1); }
        if (x < W - 1 && !visited[q + 1] && Gimg[q + 1] === gg) { visited[q + 1] = 1; stack.push(q + 1); }
      }
      if (n >= minb) blobs[gg].push(n);
    }

    var scored = [];
    groups.forEach(function (g, gi) {
      var nb = blobs[gi].length;
      if (nb < ACC_MIN_BLOBS) return;
      var sat = Math.min(1, g.chroma / ACC_SAT_FULL);
      var flatRatio = g.fsum / g.asum;
      var rep = Math.min(nb, ACC_REP_CAP) / ACC_REP_CAP;
      g.score = sat * flatRatio * rep;
      scored.push(g);
    });
    scored.sort(function (a, b) { return b.score - a.score; });
    var picks = [];
    if (scored.length) {
      var top = scored[0].score;
      for (var s = 0; s < scored.length; s++) {
        var cand = scored[s];
        if (picks.length >= ACC_PICK_MAX || cand.score < ACC_PICK_RATIO * top) break;
        var far = picks.every(function (p2) { return dist3(cand.lab, p2.lab) >= ACC_PICK_DE; });
        if (far) picks.push(cand);
      }
    }
    return picks;
  }

  // Велика копія -> палітра (спільна частина для extractPalette)
  function extractFromDetail(detail, opts) {
    var coarse = downscale(detail, COARSE_SIDE);
    var st = structure(coarse, opts);
    if (!st) return [];
    var bg = st.bg, surf = st.surf, rest = st.rest;
    var W = detail.width, H = detail.height, N = W * H, data = detail.data;
    var i;

    var valid = new Uint8Array(N);
    for (i = 0; i < N; i++) valid[i] = data[i * 4 + 3] >= 128 ? 1 : 0;
    if (opts.margins !== false) {
      var mm = marginMask(data, W, H, valid);
      for (i = 0; i < N; i++) if (mm[i]) valid[i] = 0;
    }
    var flat = flatMask(data, W, H);
    var keyOf = new Int32Array(N);
    var allMap = new Map(), flatMap = new Map(), nValid = 0;
    for (i = 0; i < N; i++) {
      var o = i * 4;
      var key = (data[o] << 16) | (data[o + 1] << 8) | data[o + 2];
      keyOf[i] = key;
      if (!valid[i]) { flat[i] = 0; continue; }
      nValid++;
      allMap.set(key, (allMap.get(key) || 0) + 1);
      if (flat[i]) flatMap.set(key, (flatMap.get(key) || 0) + 1);
      else flat[i] = 0;
    }
    var ukeys = Array.from(allMap.keys()).sort(function (a, b) { return a - b; });
    var fkeys = Array.from(flatMap.keys()).sort(function (a, b) { return a - b; });
    var fcnt = fkeys.map(function (k) { return flatMap.get(k); });
    var fall = fkeys.map(function (k) { return allMap.get(k); });
    var fIndex = new Map();
    fkeys.forEach(function (k, j) { fIndex.set(k, j); });
    var fIdx = new Int32Array(N).fill(-1);
    for (i = 0; i < N; i++) if (flat[i]) fIdx[i] = fIndex.get(keyOf[i]);

    // hex кластерів: мода плоских пікселів, найближчих до центру кластера
    var allc = [bg].concat(surf ? [surf] : [], rest);
    var M = fkeys.length;
    var flabF = fkeys.map(function (k) { return srgbToLab((k >> 16) & 255, (k >> 8) & 255, k & 255); });
    var asg = new Int32Array(M);
    for (var j = 0; j < M; j++) {
      var bc = 0, bq = Infinity;
      for (var c = 0; c < allc.length; c++) {
        var dx = flabF[j][0] - allc[c].lab[0], dy = flabF[j][1] - allc[c].lab[1], dz = flabF[j][2] - allc[c].lab[2];
        var q2 = dx * dx + dy * dy + dz * dz;
        if (q2 < bq) { bq = q2; bc = c; }
      }
      asg[j] = bc;
    }
    allc.forEach(function (cl, ci) {
      var tot = 0, best = -1, bestCnt = -1;
      for (var jj = 0; jj < M; jj++) if (asg[jj] === ci) {
        tot += fcnt[jj];
        if (fcnt[jj] > bestCnt) { bestCnt = fcnt[jj]; best = jj; }
      }
      if (tot >= FLAT_MIN_PX) cl.hex = hexOfKey(fkeys[best]);
    });

    // знаменник часток акценту: валідні пікселі, що лишилися після видалення фону/поверхні
    var resCount = nValid;
    if (st.removal) {
      var removedCenters = [bg].concat(st.surfRemoved && surf ? [surf] : []);
      resCount = 0;
      ukeys.forEach(function (k) {
        var l = srgbToLab((k >> 16) & 255, (k >> 8) & 255, k & 255);
        for (var r = 0; r < removedCenters.length; r++) if (dist3(l, removedCenters[r].lab) <= REMOVE_DE) return;
        resCount += allMap.get(k);
      });
    }

    var picks = accentPass(fkeys, fcnt, fall, bg, surf, nValid, W, H, fIdx);
    var rest2 = rest.filter(function (c) {
      var hl = hexToLab(c.hex);
      return !picks.some(function (p) { return dist3(hl, p.lab) < ACC_DROP_DE; });
    });
    assignTextRoles(rest2, bg);

    var out = [];
    var total = st.totalAll;
    if (st.removal) {
      out.push(item(bg, bg.w / total, 'background', true));
      if (surf && st.surfRemoved) out.push(item(surf, surf.w / total, 'surface', true));
    } else {
      out.push(item(bg, bg.share, 'background', false));
    }
    if (surf && !st.surfRemoved) out.push(item(surf, surf.share, 'surface', false));

    var tail = rest2.map(function (c) { return item(c, c.share, c.role, false); });
    picks.forEach(function (p) {
      tail.push({ hex: p.hex, share: Number((p.asum / resCount).toFixed(4)), role: 'accent' });
    });
    tail.sort(function (a, b) { return b.share - a.share; });
    return out.concat(tail);
  }

  function item(c, share, role, removed) {
    var d = { hex: c.hex, share: Number(share.toFixed(4)), role: role };
    if (removed) d.removed = true;
    return d;
  }

  // imageData -> палітра. Велика копія: detailSide (за замовчуванням 800) або повний розмір при hiRes.
  function extractPalette(imageData, opts) {
    opts = opts || {};
    var src = imageData;
    if (opts.rect) src = cropRect(src, opts.rect);
    var detail = opts.hiRes ? src : downscale(src, opts.detailSide || DETAIL_SIDE);
    return extractFromDetail(detail, opts);
  }

  // Завантаження картинки в imageData через canvas (браузер). Файл/Blob/ImageBitmap/HTMLImageElement.
  // maxSide = null (або Infinity) -> повний розмір. Якщо картинка з чужого домену, canvas "забруднюється".
  function imageDataFromSource(source, maxSide) {
    var full = maxSide === null || maxSide === undefined || !isFinite(maxSide);
    var p = (typeof createImageBitmap === 'function' && typeof Blob !== 'undefined' && source instanceof Blob)
      ? createImageBitmap(source)
      : Promise.resolve(source);
    return p.then(function (img) {
      var w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
      var f = full ? 1 : Math.min(1, maxSide / Math.max(w, h));
      var cw = Math.max(1, Math.round(w * f)), ch = Math.max(1, Math.round(h * f));
      var cv = document.createElement('canvas');
      cv.width = cw; cv.height = ch;
      var ctx = cv.getContext('2d', { willReadFrequently: true });
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, 0, 0, cw, ch);
      return ctx.getImageData(0, 0, cw, ch);
    });
  }

  return { extractPalette: extractPalette, downscale: downscale, cropRect: cropRect,
           imageDataFromSource: imageDataFromSource };
});
