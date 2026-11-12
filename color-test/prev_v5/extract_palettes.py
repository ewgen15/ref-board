#!/usr/bin/env python3
"""Витягування палітри з референсів (еталонна Python-версія алгоритму, v5: фон за периметром, «плоскі пікселі + мода»).

Зміна v5: фон (background) — кластер з найбільшою часткою смуги по краю кадру (товщина EDGE_BAND),
  а не найбільший кластер. Решта логіки як у v4.
Ідея v4:
  * Дві копії. Мала (COARSE_SIDE = 200 px) — кластери, частки площі, фон/поверхня/решта.
    Велика (DETAIL_SIDE = 800 px, або повний розмір з --hi-res) — точні hex, акцент, текст.
  * Піксель «плоский», якщо 8 сусідів у 3x3 майже ідентичні йому (max Δканалу <= FLAT_TOL).
    Антиаліасинг, тіні й градієнти відсіюються.
  * hex кластера = МОДА точного RGB серед плоских великої копії, найближчих до його центру
    (якщо плоских замало, береться центроїд кластера).
  * Окремий прохід акценту на великій копії: плоскі насичені точні кольори, далекі за відтінком
    від фону й поверхні; групування за схожістю; вимога >= 2 звʼязних плям мінімального розміру;
    оцінка = насиченість * плоскість * повторюваність (без порога за часткою площі). До 3 акцентів.

Приклади:
  python3 extract_palettes.py                       # усі референси -> palettes.json + preview
  python3 extract_palettes.py --hi-res              # велика копія = повний розмір
  python3 extract_palettes.py --detail-side 600     # інший розмір великої копії
  python3 extract_palettes.py --no-remove-bg        # вимкнути режим «фон окремо»
  python3 extract_palettes.py --rect 10,5,60,70     # лише прямокутник (у відсотках x,y,w,h)
  python3 extract_palettes.py --dump-bins DIR       # зберегти великі копії (RGBA) для тесту JS
  python3 extract_palettes.py --before prev_v3/palettes.json   # колонка «до» у превʼю
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from refs_util import default_ref_dir, load_ref_map  # noqa: E402

REF_DIR = default_ref_dir()

COARSE_SIDE = 200       # мала копія: кластери і частки площі
DETAIL_SIDE = 800       # велика копія: hex, акцент, текст (--hi-res -> повний розмір)
K = 8                   # кількість кластерів k-means (мала копія)
ITERS = 10
MERGE_DE = 8.0
MIN_SHARE_OUT = 0.001
SURFACE_MIN_SHARE = 0.05
TEXT_MIN_SHARE = 0.002
TEXT_MAX_SHARE = 0.30
TEXT_NEUTRAL_CHROMA = 30.0
TEXT_MIN_DE_BG = 20.0
MARGIN_WHITE = 245
MARGIN_MAX_FRAC = 0.15
EDGE_BAND = 0.03
REMOVE_BG_SHARE = 0.5
REMOVE_DE = 8.0
REMOVE_SURFACE_SHARE = 0.5
MAX_PASSES = 2
# плоскі пікселі і мода
FLAT_TOL = 4            # max Δканалу між пікселем і кожним із 8 сусідів 3x3
FLAT_MIN_PX = 8         # менше плоских пікселів у кластері -> центроїд замість моди
# акцент
ACC_MIN_CHROMA = 25.0   # насиченість (chroma Lab) мінімум
ACC_NEUTRAL_BG = 8.0    # якщо ref майже нейтральний, відтінок не визначений: далекі всі
ACC_HUE_FAR = 20.0      # відтінок далекий від фону/поверхні, якщо різниця >= 20°
ACC_FAR_DE = 50.0       # або контраст ΔE >= 50 (яскравий лайм на темному оливковому фоні)
ACC_GROUP_DE = 12.0     # родина точних кольорів (лідер-кластеризація); hex = найчастіший плоский
ACC_GROUPS_MAX = 40     # розглядаємо найбільші групи
ACC_MIN_BLOB_PX = 12
ACC_MIN_BLOB_FRAC = 0.00002    # мінімальна пляма = max(12, 0.002 % площі великої копії)
ACC_MIN_BLOBS = 2       # вимога: >= 2 звʼязні плями мінімального розміру
ACC_REP_CAP = 4         # повторюваність: min(плям, 4) / 4 (2 плями -> 0.5, 4+ -> 1)
ACC_SAT_FULL = 60.0     # chroma, з якої насиченість = 1
ACC_PICK_MAX = 3
ACC_PICK_RATIO = 0.4    # додатковий акцент має мати >= 40 % оцінки найкращого
ACC_PICK_DE = 12.0      # акценти між собою мають відрізнятися
ACC_DROP_DE = 12.0      # кластери решти, близькі до акценту, не дублюємо

D65 = np.array([0.95047, 1.0, 1.08883])
RGB2XYZ = np.array([[0.4124, 0.3576, 0.1805],
                    [0.2126, 0.7152, 0.0722],
                    [0.0193, 0.1192, 0.9505]])


def srgb_to_lab(rgb):
    """rgb: (..., 3) float у діапазоні 0..255 -> (..., 3) Lab (D65)."""
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (c @ RGB2XYZ.T) / D65
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def rel_luminance(rgb):
    c = np.asarray(rgb, float) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast_ratio(rgb1, rgb2):
    l1, l2 = rel_luminance(rgb1), rel_luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def de(a, b):
    return float(np.linalg.norm(np.asarray(a) - np.asarray(b)))


def unpack(keys):
    """Упаковані ключі 0xRRGGBB -> (n, 3) float RGB."""
    k = np.asarray(keys, np.int64)
    return np.stack([(k >> 16) & 255, (k >> 8) & 255, k & 255], axis=-1).astype(float)


def hex_of_key(k):
    return "#%02x%02x%02x" % ((int(k) >> 16) & 255, (int(k) >> 8) & 255, int(k) & 255)


def hex_of_rgb(rgb):
    return "#%02x%02x%02x" % tuple(int(math.floor(v + 0.5)) for v in np.clip(rgb, 0, 255))


def hex_to_rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def load_rgba(path, rect=None, side=DETAIL_SIDE):
    """side=None -> повний розмір. Pillow BILINEAR (браузер має canvas; паритет на дампах)."""
    im = Image.open(path).convert("RGBA")
    if rect:
        W, H = im.size
        x, y, w, h = rect
        x0, y0 = int(math.floor(W * x / 100.0)), int(math.floor(H * y / 100.0))
        x1, y1 = int(math.floor(W * (x + w) / 100.0)), int(math.floor(H * (y + h) / 100.0))
        im = im.crop((x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    if side:
        im.thumbnail((side, side), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def crop_arr(arr, rect):
    """Той самий кроп у відсотках, що в JS cropRect (для тесту паритету)."""
    H, W = arr.shape[:2]
    x, y, w, h = rect
    x0, y0 = int(math.floor(W * x / 100.0)), int(math.floor(H * y / 100.0))
    x1, y1 = int(math.floor(W * (x + w) / 100.0)), int(math.floor(H * (y + h) / 100.0))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    return arr[y0:y1, x0:x1]


def box_downscale(arr, max_side):
    """Box-фільтр (середнє по блоках) з цілочисельним округленням; та сама формула в JS."""
    H, W = arr.shape[:2]
    s = max(H, W)
    if s <= max_side:
        return arr
    f = max_side / float(s)
    nw = max(1, int(math.floor(W * f + 0.5)))
    nh = max(1, int(math.floor(H * f + 0.5)))
    y0 = (np.arange(nh) * H) // nh
    x0 = (np.arange(nw) * W) // nw
    y1 = np.append(y0[1:], H)
    x1 = np.append(x0[1:], W)
    t = np.add.reduceat(arr.astype(np.int64), y0, axis=0)
    t = np.add.reduceat(t, x0, axis=1)
    n = ((y1 - y0)[:, None] * (x1 - x0)[None, :])[..., None]
    return ((2 * t + n) // (2 * n)).astype(np.uint8)


def margin_mask(rgb, valid):
    """Біла смуга полів: білі пікселі, зв'язані з межею; якщо їх >= 15 % площі — це фон, не поля."""
    H, W = valid.shape
    N = H * W
    cand = (valid & (rgb.min(axis=2) >= MARGIN_WHITE)).ravel()
    candb = cand.astype(np.uint8).tobytes()
    seen = bytearray(N)
    border = np.unique(np.concatenate([np.arange(W), np.arange((H - 1) * W, N),
                                       np.arange(0, N, W), np.arange(W - 1, N, W)]))
    q = deque()
    for i in border.tolist():
        if candb[i]:
            seen[i] = 1
            q.append(i)
    count = len(q)
    limit = MARGIN_MAX_FRAC * N
    while q and count < limit:
        i = q.popleft()
        y, x = divmod(i, W)
        for ok, j in ((y > 0, i - W), (y < H - 1, i + W), (x > 0, i - 1), (x < W - 1, i + 1)):
            if ok and candb[j] and not seen[j]:
                seen[j] = 1
                q.append(j)
                count += 1
    if count >= limit:
        return np.zeros((H, W), bool)
    return np.frombuffer(bytes(seen), np.uint8).reshape(H, W).astype(bool)


def flat_mask(rgb, tol=FLAT_TOL):
    """Плоский піксель: усі 8 сусідів у 3x3 відрізняються max Δканалу <= tol. Межі кадру — не плоскі."""
    H, W = rgb.shape[:2]
    flat = np.zeros((H, W), bool)
    if H < 3 or W < 3:
        return flat
    c = rgb[1:-1, 1:-1]
    ok = np.ones((H - 2, W - 2), bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            nb = rgb[1 + dy:H - 1 + dy, 1 + dx:W - 1 + dx]
            ok &= np.abs(c - nb).max(axis=2) <= tol
    flat[1:-1, 1:-1] = ok
    return flat


def build_bins(arr, opts):
    """Пікселі малої копії -> кошики 5 біт: (lab, вага, середній RGB) або None, якщо порожньо."""
    H, W, _ = arr.shape
    rgb = arr[..., :3].astype(float)
    valid = arr[..., 3] >= 128
    if opts.get("margins", True):
        valid &= ~margin_mask(rgb, valid)
    weight = np.ones((H, W))
    if opts.get("center", False):
        yy, xx = np.mgrid[0:H, 0:W]
        d = np.hypot(xx - (W - 1) / 2.0, yy - (H - 1) / 2.0)
        weight = 1.0 + (1.0 - d / max(np.hypot(W / 2.0, H / 2.0), 1e-9))

    r5 = arr[..., 0].astype(np.int64) >> 3
    g5 = arr[..., 1].astype(np.int64) >> 3
    b5 = arr[..., 2].astype(np.int64) >> 3
    idx = (r5 << 10) | (g5 << 5) | b5
    iv = idx[valid]
    wv = weight[valid]
    NB = 32768
    cnt = np.bincount(iv, weights=wv, minlength=NB)
    sr = np.bincount(iv, weights=rgb[..., 0][valid] * wv, minlength=NB)
    sg = np.bincount(iv, weights=rgb[..., 1][valid] * wv, minlength=NB)
    sb = np.bincount(iv, weights=rgb[..., 2][valid] * wv, minlength=NB)
    # смуга по краю кадру (периметр): та сама товщина, що й edge_only (EDGE_BAND по кожній стороні)
    bh = max(1, int(math.floor(EDGE_BAND * H + 0.5)))
    bw = max(1, int(math.floor(EDGE_BAND * W + 0.5)))
    yy, xx = np.mgrid[0:H, 0:W]
    band = (xx < bw) | (xx >= W - bw) | (yy < bh) | (yy >= H - bh)
    bandw = np.bincount(idx[valid & band], weights=weight[valid & band], minlength=NB)
    if opts.get("edge_only", False):
        inner = (xx >= bw) & (xx < W - bw) & (yy >= bh) & (yy < H - bh)
        inner_idx = idx[valid & inner]
        has_inner = np.bincount(inner_idx, minlength=NB) > 0
        cnt = np.where(has_inner, cnt, 0.0)

    nz = np.nonzero(cnt > 0)[0]
    if nz.size == 0:
        return None
    w = cnt[nz]
    means = np.stack([sr[nz], sg[nz], sb[nz]], axis=1) / w[:, None]
    return srgb_to_lab(means), w, means, bandw[nz]


def cluster_bins(lab, w, means, bandw=None):
    """k-means (детермінований) + злиття ΔE < MERGE_DE. Повертає кластери з абсолютними вагами.
    bandw: вага пікселів смуги по краю кадру (периметр) для кожного кошика; сумується в кластері."""
    M = len(w)
    if bandw is None:
        bandw = np.zeros(M)
    k = min(K, M)
    centers = [lab[int(np.argmax(w))]]
    d2 = ((lab - centers[0]) ** 2).sum(axis=1)
    while len(centers) < k:
        score = w * d2
        j = int(np.argmax(score))
        if score[j] <= 0:
            break
        centers.append(lab[j])
        d2 = np.minimum(d2, ((lab - lab[j]) ** 2).sum(axis=1))
    C = np.array(centers)

    for _ in range(ITERS):
        dist = ((lab[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        asg = np.argmin(dist, axis=1)
        for c in range(len(C)):
            m = asg == c
            if m.any():
                C[c] = (lab[m] * w[m, None]).sum(axis=0) / w[m].sum()
    dist = ((lab[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
    asg = np.argmin(dist, axis=1)

    clusters = []
    for c in range(len(C)):
        m = asg == c
        if not m.any():
            continue
        Wc = w[m].sum()
        clusters.append({
            "lab": (lab[m] * w[m, None]).sum(axis=0) / Wc,
            "rgb": (means[m] * w[m, None]).sum(axis=0) / Wc,
            "w": Wc,
            "bandw": bandw[m].sum(),
        })

    while len(clusters) > 1:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = float(np.linalg.norm(clusters[i]["lab"] - clusters[j]["lab"]))
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best[0] >= MERGE_DE:
            break
        _, i, j = best
        a, b = clusters[i], clusters[j]
        Wc = a["w"] + b["w"]
        merged = {
            "lab": (a["lab"] * a["w"] + b["lab"] * b["w"]) / Wc,
            "rgb": (a["rgb"] * a["w"] + b["rgb"] * b["w"]) / Wc,
            "w": Wc,
            "bandw": a["bandw"] + b["bandw"],
        }
        clusters = [c for t, c in enumerate(clusters) if t not in (i, j)] + [merged]
    return clusters


def prep(c):
    c["hex"] = hex_of_rgb(c["rgb"])
    c["chroma"] = float(math.hypot(c["lab"][1], c["lab"][2]))


def finalize(clusters, total):
    for c in clusters:
        c["share"] = c["w"] / total
        prep(c)
    clusters = [c for c in clusters if c["share"] >= MIN_SHARE_OUT]
    clusters.sort(key=lambda c: -c["share"])
    return clusters


def residual(rl, rw, rm):
    if rw.size == 0:
        return []
    return finalize(cluster_bins(rl, rw, rm), float(rw.sum()))


def pick_surface(rest, bg):
    """Поверхня: найближчий до фону кластер з часткою > 5 %. Ставить роль і повертає його (або None)."""
    cands = [c for c in rest if c["share"] > SURFACE_MIN_SHARE]
    if not cands:
        return None
    s = min(cands, key=lambda c: float(np.linalg.norm(c["lab"] - bg["lab"])))
    s["role"] = "surface"
    return s


def assign_text_roles(rest, bg):
    """textPrimary / textSecondary серед решти; решта -> other."""
    for c in rest:
        c["role"] = "other"

    def de_bg(c):
        return float(np.linalg.norm(c["lab"] - bg["lab"]))

    def text_ok(c):
        return TEXT_MIN_SHARE <= c["share"] <= TEXT_MAX_SHARE and de_bg(c) >= TEXT_MIN_DE_BG

    def neutral_first(pool):
        aa = [c for c in pool if contrast_ratio(c["rgb"], bg["rgb"]) >= 4.5]
        pool = aa if aa else pool
        neutral = [c for c in pool if c["chroma"] <= TEXT_NEUTRAL_CHROMA]
        return neutral if neutral else pool

    tp = None
    cands = neutral_first([c for c in rest if text_ok(c)])
    if cands:
        tp = max(cands, key=lambda c: contrast_ratio(c["rgb"], bg["rgb"]))
        tp["role"] = "textPrimary"
    if tp is not None:
        Lbg, Lt = bg["lab"][0], tp["lab"][0]
        lo, hi = min(Lbg, Lt), max(Lbg, Lt)
        mid = (Lbg + Lt) / 2.0
        cands = neutral_first([c for c in rest if c["role"] == "other" and text_ok(c)
                               and lo < c["lab"][0] < hi])
        if cands:
            min(cands, key=lambda c: abs(c["lab"][0] - mid))["role"] = "textSecondary"


def structure(arr, opts):
    """Мала копія: кластери, фон і поверхня (з видаленням, якщо фон > 50 %), решта.
    Фон (v5) — кластер з найбільшою часткою смуги по периметру кадру (при нічиїй — найбільший)."""
    b = build_bins(arr, opts)
    if b is None:
        return None
    lab, w, means, bandw = b
    total_all = float(w.sum())
    clusters = finalize(cluster_bins(lab, w, means, bandw), total_all)
    if not clusters:
        return None
    per = max(range(len(clusters)), key=lambda i: clusters[i]["bandw"])
    bg = clusters.pop(per)
    clusters.insert(0, bg)
    if not opts.get("remove_bg", True) or bg["share"] <= REMOVE_BG_SHARE:
        bg["role"] = "background"
        rest = clusters[1:]
        surf = pick_surface(rest, bg)
        if surf is not None:
            rest = [c for c in rest if c is not surf]
        return {"bg": bg, "surf": surf, "surf_removed": False, "rest": rest,
                "removal": False, "total_all": total_all}

    keep = np.linalg.norm(lab - bg["lab"], axis=1) > REMOVE_DE
    rl, rw, rm = lab[keep], w[keep], means[keep]
    rest = residual(rl, rw, rm)
    surf, surf_removed = None, False
    if rest and rest[0]["share"] > REMOVE_SURFACE_SHARE:
        surf, surf_removed = rest[0], True
        keep2 = np.linalg.norm(rl - surf["lab"], axis=1) > REMOVE_DE
        rl, rw, rm = rl[keep2], rw[keep2], rm[keep2]
        rest = residual(rl, rw, rm)
    else:
        surf = pick_surface(rest, bg)
        if surf is not None:
            rest = [c for c in rest if c is not surf]
    bg["role"] = "background"
    return {"bg": bg, "surf": surf, "surf_removed": surf_removed, "rest": rest,
            "removal": True, "total_all": total_all}


def far_mask(flab, ref):
    """True там, де колір далекий від ref: за відтінком (>= ACC_HUE_FAR) або за контрастом
    (ΔE >= ACC_FAR_DE, наприклад яскравий лайм на темному оливковому фоні). Майже нейтральний
    ref: далекі всі кольори."""
    rl = ref["lab"]
    if math.hypot(rl[1], rl[2]) < ACC_NEUTRAL_BG:
        return np.ones(len(flab), bool)
    h = np.degrees(np.arctan2(flab[:, 2], flab[:, 1]))
    hr = math.degrees(math.atan2(rl[2], rl[1]))
    d = np.abs(h - hr) % 360.0
    d = np.minimum(d, 360.0 - d)
    dE = np.linalg.norm(flab - rl, axis=1)
    return (d >= ACC_HUE_FAR) | (dE >= ACC_FAR_DE)


def accent_pass(fkeys, fcnt, ukeys, ucnt, bg, surf, n_valid, shape, flat_pix, flat_keys):
    """Пошук малого акценту на великій копії. Повертає до ACC_PICK_MAX груп (dict).
    flat_pix/flat_keys: індекси плоских пікселів (у плоскому масиві) і їхні упаковані ключі."""
    if fkeys.size == 0:
        return []
    flab = srgb_to_lab(unpack(fkeys))
    fch = np.hypot(flab[:, 1], flab[:, 2])
    ok = (fch >= ACC_MIN_CHROMA) & far_mask(flab, bg)
    if surf is not None:
        ok &= far_mask(flab, surf)
    fall = ucnt[np.searchsorted(ukeys, fkeys)]
    order = np.argsort(-fcnt, kind="stable")          # за частотою, при рівності — за ключем
    groups = []
    gid_f = np.full(len(fkeys), -1, np.int64)
    for i in order.tolist():
        if not ok[i] or fcnt[i] < 2:
            continue
        j = -1
        if groups:
            G = np.array([g["lab"] for g in groups])
            d = np.linalg.norm(G - flab[i], axis=1)
            jj = int(np.argmin(d))
            if d[jj] <= ACC_GROUP_DE:
                j = jj
        if j >= 0:
            groups[j]["fsum"] += int(fcnt[i])
            groups[j]["asum"] += int(fall[i])
        else:
            if len(groups) >= ACC_GROUPS_MAX:
                continue
            groups.append({"lab": flab[i], "hex": hex_of_key(fkeys[i]), "chroma": float(fch[i]),
                           "fsum": int(fcnt[i]), "asum": int(fall[i])})
            j = len(groups) - 1
        gid_f[i] = j
    if not groups:
        return []

    H, W = shape
    Gimg = np.full(H * W, -1, np.int64)
    if flat_pix.size:
        jpos = np.searchsorted(fkeys, flat_keys[flat_pix])
        Gimg[flat_pix] = gid_f[jpos]
    Gl = Gimg.tolist()
    minb = max(ACC_MIN_BLOB_PX, int(math.floor(ACC_MIN_BLOB_FRAC * n_valid + 0.5)))
    seen = bytearray(H * W)
    blobs = [[] for _ in groups]
    for p in np.nonzero(Gimg >= 0)[0].tolist():
        if seen[p]:
            continue
        g = Gl[p]
        seen[p] = 1
        stack = [p]
        n = 0
        while stack:
            q = stack.pop()
            n += 1
            y, x = divmod(q, W)
            for ok_, j2 in ((y > 0, q - W), (y < H - 1, q + W), (x > 0, q - 1), (x < W - 1, q + 1)):
                if ok_ and not seen[j2] and Gl[j2] == g:
                    seen[j2] = 1
                    stack.append(j2)
        if n >= minb:
            blobs[g].append(n)

    scored = []
    for gi, g in enumerate(groups):
        nb = len(blobs[gi])
        if nb < ACC_MIN_BLOBS:
            continue
        sat = min(1.0, g["chroma"] / ACC_SAT_FULL)
        flat_ratio = g["fsum"] / float(g["asum"])
        rep = min(nb, ACC_REP_CAP) / float(ACC_REP_CAP)
        g["score"] = sat * flat_ratio * rep
        g["nblobs"] = nb
        scored.append(g)
    scored.sort(key=lambda g: -g["score"])
    picks = []
    if scored:
        top = scored[0]["score"]
        for g in scored:
            if len(picks) >= ACC_PICK_MAX or g["score"] < ACC_PICK_RATIO * top:
                break
            if all(de(g["lab"], p["lab"]) >= ACC_PICK_DE for p in picks):
                picks.append(g)
    return picks


def extract_from_detail(detail, opts=None):
    """detail: (H, W, 4) uint8 RGBA, велика копія. Повертає список {hex, share, role[, removed]}."""
    opts = opts or {}
    coarse = box_downscale(detail, COARSE_SIDE)
    st = structure(coarse, opts)
    if st is None:
        return []
    bg, surf, rest = st["bg"], st["surf"], st["rest"]
    removal, surf_removed, total_all = st["removal"], st["surf_removed"], st["total_all"]

    H, W = detail.shape[:2]
    rgb = detail[..., :3].astype(np.int16)
    valid = detail[..., 3] >= 128
    if opts.get("margins", True):
        valid &= ~margin_mask(rgb, valid)
    flat = flat_mask(rgb) & valid
    key = (rgb[..., 0].astype(np.int64) << 16) | (rgb[..., 1].astype(np.int64) << 8) | rgb[..., 2].astype(np.int64)
    ukeys, ucnt = np.unique(key[valid], return_counts=True)
    fkeys, fcnt = np.unique(key[flat], return_counts=True)
    n_valid = int(ucnt.sum())

    # hex кластерів: мода плоских пікселів, найближчих до центру кластера
    allc = [bg] + ([surf] if surf is not None else []) + rest
    if fkeys.size:
        C = np.array([c["lab"] for c in allc])
        flab = srgb_to_lab(unpack(fkeys))
        asg = np.argmin(((flab[:, None, :] - C[None, :, :]) ** 2).sum(axis=2), axis=1)
    for i, c in enumerate(allc):
        idx = np.nonzero(asg == i)[0] if fkeys.size else np.zeros(0, np.int64)
        tot = int(fcnt[idx].sum()) if idx.size else 0
        c["flat_n"] = tot
        if tot >= FLAT_MIN_PX:
            c["hex"] = hex_of_key(fkeys[idx[int(np.argmax(fcnt[idx]))]])
        # інакше лишається центроїд (hex з prep)

    # знаменник часток акценту: валідні пікселі, що лишилися після видалення фону/поверхні
    res_count = n_valid
    if removal:
        removed_centers = [bg] + ([surf] if surf_removed else [])
        ul = srgb_to_lab(unpack(ukeys))
        keep = np.ones(len(ukeys), bool)
        for rc in removed_centers:
            keep &= np.linalg.norm(ul - rc["lab"], axis=1) > REMOVE_DE
        res_count = int(ucnt[keep].sum())

    picks = accent_pass(fkeys, fcnt, ukeys, ucnt, bg, surf, n_valid, (H, W),
                        np.nonzero(flat.ravel())[0], key.ravel())

    # решта без дублів акценту, потім ролі тексту
    pick_labs = [p["lab"] for p in picks]
    rest2 = [c for c in rest if not any(de(hex_to_lab(c["hex"]), pl) < ACC_DROP_DE for pl in pick_labs)]
    assign_text_roles(rest2, bg)

    out_items = []
    if removal:
        out_items.append(_item(bg, bg["w"] / total_all, "background", removed=True))
        if surf is not None and surf_removed:
            out_items.append(_item(surf, surf["w"] / total_all, "surface", removed=True))
    else:
        out_items.append(_item(bg, bg["share"], "background"))
    if surf is not None and not surf_removed:
        out_items.append(_item(surf, surf["share"], "surface"))

    tail = [_item(c, c["share"], c["role"]) for c in rest2]
    for p in picks:
        tail.append({"hex": p["hex"], "share": round(p["asum"] / float(res_count), 4), "role": "accent"})
    tail.sort(key=lambda d: -d["share"])
    return out_items + tail


def hex_to_lab(h):
    return srgb_to_lab(np.array(hex_to_rgb(h), float))


def _item(c, share, role, removed=False):
    d = {"hex": c["hex"], "share": round(float(share), 4), "role": role}
    if removed:
        d["removed"] = True
    return d


def extract_file(path, rect=None, opts=None, side=DETAIL_SIDE):
    return extract_from_detail(load_rgba(path, rect, side), opts)


def list_refs(ref_dir=None):
    """{ref_NN: шлях до файлу}; відповідність беруть з refs.local.json (див. refs_util.py)."""
    return load_ref_map(ref_dir or REF_DIR)


def _draw_strip(d, x0, y, pal, small):
    removed_sum = sum(c["share"] for c in pal if c.get("removed"))
    factor = 1.0 - removed_sum
    x = x0
    BAR_W = 1020
    for c in pal:
        disp = c["share"] if c.get("removed") else c["share"] * factor
        w = max(1, int(round(BAR_W * disp)))
        if x + w > x0 + BAR_W:
            w = x0 + BAR_W - x
        if w <= 0:
            break
        rgb = tuple(int(c["hex"][i:i + 2], 16) for i in (1, 3, 5))
        d.rectangle([x, y, x + w - 1, y + 44], fill=rgb, outline=(200, 200, 200))
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        tc = (0, 0, 0) if lum > 140 else (255, 255, 255)
        if w >= 90:
            d.text((x + 5, y + 4), c["hex"], fill=tc, font=small)
            d.text((x + 5, y + 22), c["role"] + (" (вилуч.)" if c.get("removed") else ""), fill=tc, font=small)
        x += w


def _legend(d, x0, y, pal, small):
    lx, ly = x0, y
    for c in pal:
        tag = " вилуч." if c.get("removed") else " від залишку"
        if not c.get("removed") and not any(p.get("removed") for p in pal):
            tag = ""
        label = "%s %s %.1f%%%s" % (c["role"], c["hex"], c["share"] * 100, tag)
        tw = d.textlength(label + "    ", font=small)
        if lx + tw > x0 + 1020:
            lx, ly = x0, ly + 16
        d.text((lx, ly), label, fill=(40, 40, 40), font=small)
        lx += tw


def make_preview(before, after, out_path,
                 before_label="до (v4)", after_label="після (v5)"):
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 15)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except OSError:
        font = small = ImageFont.load_default()
    THUMB_W, THUMB_H = 150, 110
    BAR_X = 180
    ROW_H = 226
    names = list(after.keys())
    img = Image.new("RGB", (1220, ROW_H * len(names) + 40), (246, 246, 246))
    d = ImageDraw.Draw(img)
    d.text((12, 10), "Палітри: %s і %s. Смуга: частка від усієї картинки; "
                     "у легенді для залишку — частка від залишку." % (before_label, after_label),
           fill=(20, 20, 20), font=font)
    y = 40
    for name in names:
        bg = next((c["hex"] for c in after[name] if c["role"] == "background"), "#cccccc")
        d.rectangle([12, y + 4, 12 + THUMB_W, y + 4 + THUMB_H], fill=bg, outline=(200, 200, 200))  # лише колір фону, без картинки
        d.text((12, y + THUMB_H + 8), name, fill=(60, 60, 60), font=small)
        d.text((BAR_X, y), before_label, fill=(90, 90, 90), font=small)
        _draw_strip(d, BAR_X, y + 16, before.get(name, []), small)
        _legend(d, BAR_X, y + 64, before.get(name, []), small)
        d.text((BAR_X, y + 100), after_label, fill=(90, 90, 90), font=small)
        _draw_strip(d, BAR_X, y + 116, after[name], small)
        _legend(d, BAR_X, y + 164, after[name], small)
        y += ROW_H
    img.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "palettes.json"))
    ap.add_argument("--preview", default=os.path.join(HERE, "palettes_preview.png"))
    ap.add_argument("--before", help="JSON з попередньою версією для колонки «до» у превʼю")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--no-remove-bg", action="store_true", help="вимкнути режим «фон окремо»")
    ap.add_argument("--no-margins", action="store_true", help="не відсіювати білі поля")
    ap.add_argument("--center", action="store_true", help="зважити центр малої копії")
    ap.add_argument("--edge-only", action="store_true", help="ігнорувати кольори лише на межі")
    ap.add_argument("--rect", help="x,y,w,h у відсотках")
    ap.add_argument("--detail-side", type=int, default=DETAIL_SIDE, help="довша сторона великої копії")
    ap.add_argument("--hi-res", action="store_true", help="велика копія = повний розмір")
    ap.add_argument("--dump-bins", metavar="DIR", help="зберегти великі копії (RGBA) для тесту JS")
    a = ap.parse_args()

    rect = [float(v) for v in a.rect.split(",")] if a.rect else None
    side = None if a.hi_res else a.detail_side
    opts = {"margins": not a.no_margins, "center": a.center, "edge_only": a.edge_only,
            "remove_bg": not a.no_remove_bg}
    results = {rid: extract_file(p, rect, opts, side) for rid, p in list_refs().items()}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("записано:", a.out, len(results), "референсів")
    for name, pal in results.items():
        print("%-40s %s" % (name[:40], " ".join(
            "%s%s:%s:%.4f" % (c["role"][:4], "*" if c.get("removed") else "", c["hex"], c["share"]) for c in pal)))
    if not a.no_preview:
        before = {}
        if a.before and os.path.exists(a.before):
            before = json.load(open(a.before, encoding="utf-8"))
        make_preview(before, results, a.preview)
        print("превʼю:", a.preview)
    if a.dump_bins:
        os.makedirs(a.dump_bins, exist_ok=True)
        for rid, path in list_refs().items():
            arr = load_rgba(path, None, side)
            base = rid
            arr.tofile(os.path.join(a.dump_bins, base + ".bin"))
            variants = {
                "default": extract_from_detail(arr, {"remove_bg": True}),
                "noRemoveBg": extract_from_detail(arr, {"remove_bg": False}),
                "center": extract_from_detail(arr, {"center": True}),
                "edgeOnly": extract_from_detail(arr, {"edge_only": True}),
                "noMargins": extract_from_detail(arr, {"margins": False}),
                "rect": extract_from_detail(crop_arr(arr, [10, 5, 60, 70]), {}),
            }
            with open(os.path.join(a.dump_bins, base + ".json"), "w") as f:
                json.dump({"width": int(arr.shape[1]), "height": int(arr.shape[0]),
                           "rect": [10, 5, 60, 70], "variants": variants}, f)
        print("дампи:", a.dump_bins)


if __name__ == "__main__":
    main()
