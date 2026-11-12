/* Витягування палітри з референсів. Чистий JS, без залежностей (v3).
 * Логіка повторює extract_palettes.py (еталон).
 *
 *   extractPalette(imageData, opts) -> [{hex, share, role[, removed]}, ...]
 *   imageData: {data: Uint8ClampedArray RGBA, width, height}
 *   opts: {rect: {x,y,w,h} у відсотках, margins: true, center: false, edgeOnly: false,
 *          removeBackground: true, maxSide: 200}
 *
 * removeBackground (за замовчуванням увімкнено): якщо фон > 50 % площі, його пікселі (ΔE < 8)
 * прибираються і решта аналізується окремо; частки решти відносно залишку. Другий прохід —
 * якщо домінує поверхня (> 50 % залишку). Максимум 2 проходи. Фон записується з removed: true.
 *
 * Браузер: <script src="extract-colors.js"></script>, далі ColorExtract.extractPalette(...)
 *          ColorExtract.imageDataFromSource(fileOrBlob, 200) -> Promise<imageData>
 * Node:    const CE = require('./extract-colors.js');
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ColorExtract = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var K = 8, ITERS = 10, MERGE_DE = 8.0, MIN_SHARE_OUT = 0.001;
  var SURFACE_MIN_SHARE = 0.05, ACCENT_MIN_SHARE = 0.01, ACCENT_MIN_CHROMA = 25.0;
  var TEXT_MIN_SHARE = 0.002, TEXT_MAX_SHARE = 0.30, TEXT_NEUTRAL_CHROMA = 30.0;
  var TEXT_MIN_DE_BG = 20.0, MARGIN_WHITE = 245, MARGIN_MAX_FRAC = 0.15, EDGE_BAND = 0.03;
  var REMOVE_BG_SHARE = 0.5, REMOVE_DE = 8.0, REMOVE_SURFACE_SHARE = 0.5, MAX_PASSES = 2;
  var MAX_SIDE = 200, NB = 32768;

  var D65X = 0.95047, D65Z = 1.08883;

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

  // Просте зменшення (box filter), якщо картинка більша за maxSide. У браузері краще
  // малювати на canvas (imageDataFromSource); тут — для node без canvas.
  function downscale(src, maxSide) {
    var W = src.width, H = src.height;
    var s = Math.max(W, H);
    if (s <= maxSide) return src;
    var f = maxSide / s;
    var nw = Math.max(1, Math.round(W * f)), nh = Math.max(1, Math.round(H * f));
    var out = new Uint8ClampedArray(nw * nh * 4);
    for (var y = 0; y < nh; y++) {
      var sy0 = Math.floor(y * H / nh), sy1 = Math.max(sy0 + 1, Math.floor((y + 1) * H / nh));
      for (var x = 0; x < nw; x++) {
        var sx0 = Math.floor(x * W / nw), sx1 = Math.max(sx0 + 1, Math.floor((x + 1) * W / nw));
        var acc = [0, 0, 0, 0], n = 0;
        for (var yy = sy0; yy < sy1; yy++)
          for (var xx = sx0; xx < sx1; xx++) {
            var o = (yy * W + xx) * 4;
            acc[0] += src.data[o]; acc[1] += src.data[o + 1]; acc[2] += src.data[o + 2]; acc[3] += src.data[o + 3];
            n++;
          }
        var d = (y * nw + x) * 4;
        for (var c = 0; c < 4; c++) out[d + c] = Math.round(acc[c] / n);
      }
    }
    return { data: out, width: nw, height: nh };
  }

  // Біла смуга полів: білі пікселі, зв'язані з межею; якщо їх >= 15 % площі — це фон, не поля.
  function marginMask(data, W, H, valid) {
    var seen = new Uint8Array(W * H), cand = new Uint8Array(W * H), q = new Int32Array(W * H);
    for (var i = 0; i < W * H; i++) {
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
    if (tail / (W * H) >= MARGIN_MAX_FRAC) return new Uint8Array(W * H);
    return seen;
  }

  // Пікселі -> кошики 5 біт: {lab: [[L,a,b]...], w: [...], means: [[r,g,b]...]} або null
  function buildBins(src, opts) {
    var W = src.width, H = src.height, data = src.data, N = W * H;
    var valid = new Uint8Array(N);
    for (var i = 0; i < N; i++) valid[i] = data[i * 4 + 3] >= 128 ? 1 : 0;
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
    var idxOf = new Int32Array(N);
    for (i = 0; i < N; i++) {
      var o = i * 4;
      idxOf[i] = ((data[o] >> 3) << 10) | ((data[o + 1] >> 3) << 5) | (data[o + 2] >> 3);
      if (!valid[i]) continue;
      var wv = weight[i], b = idxOf[i];
      cnt[b] += wv; sr[b] += data[o] * wv; sg[b] += data[o + 1] * wv; sb[b] += data[o + 2] * wv;
    }
    if (opts.edgeOnly) {
      var bh = Math.max(1, roundHalfUp(EDGE_BAND * H)), bw = Math.max(1, roundHalfUp(EDGE_BAND * W));
      var hasInner = new Uint8Array(NB);
      for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) {
          var pi = y * W + x;
          if (valid[pi] && x >= bw && x < W - bw && y >= bh && y < H - bh) hasInner[idxOf[pi]] = 1;
        }
      for (var bi = 0; bi < NB; bi++) if (!hasInner[bi]) cnt[bi] = 0;
    }
    var lab = [], w = [], means = [];
    for (var bk = 0; bk < NB; bk++) {
      if (cnt[bk] > 0) {
        var mr = [sr[bk] / cnt[bk], sg[bk] / cnt[bk], sb[bk] / cnt[bk]];
        means.push(mr);
        lab.push(srgbToLab(mr[0], mr[1], mr[2]));
        w.push(cnt[bk]);
      }
    }
    if (!w.length) return null;
    return { lab: lab, w: w, means: means };
  }

  // k-means (детермінований) + злиття ΔE < MERGE_DE. Кластери з абсолютними вагами.
  function clusterBins(lab, w, means) {
    var M = w.length, k = Math.min(K, M);
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
      var acc = { lab: [0, 0, 0], rgb: [0, 0, 0], w: 0 }, any = false;
      for (var m3 = 0; m3 < M; m3++) if (asg[m3] === c3) {
        any = true;
        var w3 = w[m3];
        for (var t = 0; t < 3; t++) { acc.lab[t] += lab[m3][t] * w3; acc.rgb[t] += means[m3][t] * w3; }
        acc.w += w3;
      }
      if (!any) continue;
      clusters.push({ lab: acc.lab.map(function (v) { return v / acc.w; }),
                      rgb: acc.rgb.map(function (v) { return v / acc.w; }), w: acc.w });
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
        w: Wc
      };
      clusters = clusters.filter(function (_, t) { return t !== bI && t !== bJ; }).concat([merged]);
    }
    return clusters;
  }

  function prep(c) {
    c.hex = '#' + hex2(c.rgb[0]) + hex2(c.rgb[1]) + hex2(c.rgb[2]);
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

  function item(c, share, role, removed) {
    var d = { hex: c.hex, share: Number(share.toFixed(4)), role: role };
    if (removed) d.removed = true;
    return d;
  }

  // Ролі для кластерів rest; bg — лише еталон для відстаней і контрасту
  function assignRoles(rest, bg, surfaceTaken) {
    rest.forEach(function (c) { c.role = 'other'; });
    function deBg(c) { return dist3(c.lab, bg.lab); }
    function free(arr) { return arr.filter(function (c) { return c.role === 'other'; }); }
    function pickMin(arr, fn) {
      var best = null, bv = Infinity;
      arr.forEach(function (c) { var v = fn(c); if (v < bv) { bv = v; best = c; } });
      return best;
    }
    function pickMax(arr, fn) {
      var best = null, bv = -Infinity;
      arr.forEach(function (c) { var v = fn(c); if (v > bv) { bv = v; best = c; } });
      return best;
    }

    if (!surfaceTaken) {
      var sc = rest.filter(function (c) { return c.share > SURFACE_MIN_SHARE; });
      if (sc.length) pickMin(sc, deBg).role = 'surface';
    }
    rest = free(rest);

    var ac = rest.filter(function (c) { return c.share >= ACCENT_MIN_SHARE && c.chroma >= ACCENT_MIN_CHROMA; });
    if (ac.length) pickMax(ac, function (c) { return c.chroma; }).role = 'accent';
    rest = free(rest);

    function textOk(c) { return c.share >= TEXT_MIN_SHARE && c.share <= TEXT_MAX_SHARE && deBg(c) >= TEXT_MIN_DE_BG; }
    function neutralFirst(pool) {
      var aa = pool.filter(function (c) { return contrastRatio(c.rgb, bg.rgb) >= 4.5; });
      if (aa.length) pool = aa;
      var neutral = pool.filter(function (c) { return c.chroma <= TEXT_NEUTRAL_CHROMA; });
      return neutral.length ? neutral : pool;
    }

    var tp = null;
    var tc = neutralFirst(rest.filter(textOk));
    if (tc.length) {
      tp = pickMax(tc, function (c) { return contrastRatio(c.rgb, bg.rgb); });
      tp.role = 'textPrimary';
    }
    rest = free(rest);

    if (tp) {
      var lo = Math.min(bg.lab[0], tp.lab[0]), hi = Math.max(bg.lab[0], tp.lab[0]);
      var mid = (bg.lab[0] + tp.lab[0]) / 2;
      var sec = neutralFirst(rest.filter(function (c) { return textOk(c) && c.lab[0] > lo && c.lab[0] < hi; }));
      if (sec.length) pickMin(sec, function (c) { return Math.abs(c.lab[0] - mid); }).role = 'textSecondary';
    }
  }

  function extractPalette(imageData, opts) {
    opts = opts || {};
    var src = imageData;
    if (opts.rect) src = cropRect(src, opts.rect);
    src = downscale(src, opts.maxSide || MAX_SIDE);
    var b = buildBins(src, opts);
    if (!b) return [];
    var lab = b.lab, w = b.w, means = b.means;
    var totalAll = w.reduce(function (s, v) { return s + v; }, 0);
    var clusters = finalize(clusterBins(lab, w, means), totalAll);
    if (!clusters.length) return [];
    var bg = clusters[0];

    if (opts.removeBackground === false || bg.share <= REMOVE_BG_SHARE) {
      bg.role = 'background';
      assignRoles(clusters.slice(1), bg, false);
      return clusters.map(function (c) { return item(c, c.share, c.role, false); });
    }

    var removed = [{ c: bg, role: 'background' }];
    var keep = [];
    for (var i = 0; i < lab.length; i++) if (dist3(lab[i], bg.lab) > REMOVE_DE) keep.push(i);
    var rl = keep.map(function (i) { return lab[i]; }), rw = keep.map(function (i) { return w[i]; }),
        rm = keep.map(function (i) { return means[i]; });
    var rest = residual(rl, rw, rm);
    var surfaceTaken = false;
    if (rest.length && rest[0].share > REMOVE_SURFACE_SHARE && removed.length < MAX_PASSES) {
      var sf = rest[0];
      removed.push({ c: sf, role: 'surface' });
      surfaceTaken = true;
      var keep2 = [];
      for (i = 0; i < rl.length; i++) if (dist3(rl[i], sf.lab) > REMOVE_DE) keep2.push(i);
      rl = keep2.map(function (j) { return rl[j]; });
      rw = keep2.map(function (j) { return rw[j]; });
      rm = keep2.map(function (j) { return rm[j]; });
      rest = residual(rl, rw, rm);
    }

    assignRoles(rest, bg, surfaceTaken);
    var out = removed.map(function (r) { return item(r.c, r.c.w / totalAll, r.role, true); });
    rest.forEach(function (c) { out.push(item(c, c.share, c.role, false)); });
    return out;
  }

  // Завантаження картинки в imageData через canvas (браузер). Файл/Blob/ImageBitmap/HTMLImageElement.
  // Якщо картинка з чужого домену, canvas "забруднюється" і getImageData кидає помилку.
  function imageDataFromSource(source, maxSide) {
    maxSide = maxSide || MAX_SIDE;
    var p = (typeof createImageBitmap === 'function' && typeof Blob !== 'undefined' && source instanceof Blob)
      ? createImageBitmap(source)
      : Promise.resolve(source);
    return p.then(function (img) {
      var w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
      var f = Math.min(1, maxSide / Math.max(w, h));
      var cw = Math.max(1, Math.round(w * f)), ch = Math.max(1, Math.round(h * f));
      var cv = document.createElement('canvas');
      cv.width = cw; cv.height = ch;
      var ctx = cv.getContext('2d', { willReadFrequently: true });
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, 0, 0, cw, ch);
      return ctx.getImageData(0, 0, cw, ch);
    });
  }

  return { extractPalette: extractPalette, downscale: downscale, cropRect: cropRect, imageDataFromSource: imageDataFromSource };
});
