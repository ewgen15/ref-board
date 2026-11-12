#!/usr/bin/env python3
"""Витягування палітри з референсів (еталонна Python-версія алгоритму, v3).

Приклади:
  python3 extract_palettes.py                      # усі референси -> palettes.json + preview
  python3 extract_palettes.py --no-remove-bg       # вимкнути режим «фон окремо»
  python3 extract_palettes.py --no-margins         # не відсіювати білі поля
  python3 extract_palettes.py --center             # зважити центр
  python3 extract_palettes.py --edge-only          # ігнорувати кольори лише на межі
  python3 extract_palettes.py --rect 10,10,80,80   # лише прямокутник (у відсотках x,y,w,h)
  python3 extract_palettes.py --dump-bins DIR      # зберегти зменшені RGBA для тесту JS

Режим removeBackground (за замовчуванням): якщо фон > 50 % площі, його пікселі (ΔE < 8 від
центроїда фону) прибираються, решта аналізується окремо. Частки решти — відносно залишку.
Другий прохід: якщо домінує поверхня (> 50 % залишку), її теж прибираємо. Максимум 2 проходи.
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

MAX_SIDE = 200          # зменшення по довшій стороні
K = 8                   # кількість кластерів k-means
ITERS = 10              # ітерацій k-means
MERGE_DE = 8.0          # злиття кластерів ближче за ΔE (CIE76)
MIN_SHARE_OUT = 0.001   # відкидаємо шум < 0.1 % (від загального або від залишку)
SURFACE_MIN_SHARE = 0.05
ACCENT_MIN_SHARE = 0.01          # частка від залишку (або від картинки, якщо фон не видаляли); верхньої межі немає
ACCENT_MIN_CHROMA = 25.0
TEXT_MIN_SHARE = 0.002
TEXT_MAX_SHARE = 0.30   # текст не займає більше третини площі (від залишку в режимі видалення)
TEXT_NEUTRAL_CHROMA = 30.0
TEXT_MIN_DE_BG = 20.0   # текст не може майже збігатися з фоном
MARGIN_WHITE = 245      # поріг «ідеально білого» для полів
MARGIN_MAX_FRAC = 0.15  # біла область більша за це — це фон, а не поля
EDGE_BAND = 0.03        # частка ширини/висоти для «межі» (варіант edge-only)
REMOVE_BG_SHARE = 0.5   # фон > 50 % -> режим видалення фону
REMOVE_DE = 8.0         # піксели ближче за ΔE до видаленого кольору прибираються (10 вже з'їдало білі картки)
REMOVE_SURFACE_SHARE = 0.5  # другий прохід: поверхня > 50 % залишку
MAX_PASSES = 2

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


def load_rgba(path, rect=None, max_side=MAX_SIDE):
    im = Image.open(path).convert("RGBA")
    if rect:
        W, H = im.size
        x, y, w, h = rect
        x0, y0 = int(math.floor(W * x / 100.0)), int(math.floor(H * y / 100.0))
        x1, y1 = int(math.floor(W * (x + w) / 100.0)), int(math.floor(H * (y + h) / 100.0))
        im = im.crop((x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    im.thumbnail((max_side, max_side), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def crop_arr(arr, rect):
    """Той самий кроп у відсотках, що в JS cropRect (для тесту паритету)."""
    H, W = arr.shape[:2]
    x, y, w, h = rect
    x0, y0 = int(math.floor(W * x / 100.0)), int(math.floor(H * y / 100.0))
    x1, y1 = int(math.floor(W * (x + w) / 100.0)), int(math.floor(H * (y + h) / 100.0))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    return arr[y0:y1, x0:x1]


def margin_mask(rgb, valid):
    """Біла смуга полів: білі пікселі, зв'язані з межею зображення. Повертає True для полів."""
    H, W = valid.shape
    cand = valid & (rgb.min(axis=2) >= MARGIN_WHITE)
    seen = np.zeros((H, W), bool)
    q = deque()
    for y in range(H):
        for x in range(W):
            if (y in (0, H - 1) or x in (0, W - 1)) and cand[y, x]:
                seen[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < H and 0 <= nx < W and cand[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                q.append((ny, nx))
    if seen.sum() / float(H * W) >= MARGIN_MAX_FRAC:
        return np.zeros((H, W), bool)
    return seen


def build_bins(arr, opts):
    """Пікселі -> кошики 5 біт: (lab, вага, середній RGB) або None, якщо порожньо."""
    H, W, _ = arr.shape
    rgb = arr[..., :3].astype(float)
    valid = arr[..., 3] >= 128                         # прозорі пікселі ігноруємо
    if opts.get("margins", True):
        valid &= ~margin_mask(rgb, valid)
    weight = np.ones((H, W))
    if opts.get("center", False):                      # центр важить більше (1..2)
        yy, xx = np.mgrid[0:H, 0:W]
        d = np.hypot(xx - (W - 1) / 2.0, yy - (H - 1) / 2.0)
        weight = 1.0 + (1.0 - d / max(np.hypot(W / 2.0, H / 2.0), 1e-9))

    r5 = arr[..., 0].astype(np.int64) >> 3
    g5 = arr[..., 1].astype(np.int64) >> 3
    b5 = arr[..., 2].astype(np.int64) >> 3
    idx = (r5 << 10) | (g5 << 5) | b5                  # 5 біт на канал: 32768 кошиків
    iv = idx[valid]
    wv = weight[valid]
    NB = 32768
    cnt = np.bincount(iv, weights=wv, minlength=NB)
    sr = np.bincount(iv, weights=rgb[..., 0][valid] * wv, minlength=NB)
    sg = np.bincount(iv, weights=rgb[..., 1][valid] * wv, minlength=NB)
    sb = np.bincount(iv, weights=rgb[..., 2][valid] * wv, minlength=NB)
    if opts.get("edge_only", False):                   # кошики, що є лише на межі
        bh = max(1, int(math.floor(EDGE_BAND * H + 0.5)))
        bw = max(1, int(math.floor(EDGE_BAND * W + 0.5)))
        yy, xx = np.mgrid[0:H, 0:W]
        inner = (xx >= bw) & (xx < W - bw) & (yy >= bh) & (yy < H - bh)
        inner_idx = idx[valid & inner]
        has_inner = np.bincount(inner_idx, minlength=NB) > 0
        cnt = np.where(has_inner, cnt, 0.0)

    nz = np.nonzero(cnt > 0)[0]
    if nz.size == 0:
        return None
    w = cnt[nz]
    means = np.stack([sr[nz], sg[nz], sb[nz]], axis=1) / w[:, None]
    return srgb_to_lab(means), w, means


def cluster_bins(lab, w, means):
    """k-means (детермінований) + злиття ΔE < MERGE_DE. Повертає кластери з абсолютними вагами."""
    M = len(w)
    k = min(K, M)

    # детермінована ініціалізація: найважчий кошик, далі найдальший (з вагою)
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
        })

    # злиття кластерів, ближчих за MERGE_DE (CIE76)
    while len(clusters) > 1:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                de = float(np.linalg.norm(clusters[i]["lab"] - clusters[j]["lab"]))
                if best is None or de < best[0]:
                    best = (de, i, j)
        if best[0] >= MERGE_DE:
            break
        _, i, j = best
        a, b = clusters[i], clusters[j]
        Wc = a["w"] + b["w"]
        merged = {
            "lab": (a["lab"] * a["w"] + b["lab"] * b["w"]) / Wc,
            "rgb": (a["rgb"] * a["w"] + b["rgb"] * b["w"]) / Wc,
            "w": Wc,
        }
        clusters = [c for t, c in enumerate(clusters) if t not in (i, j)] + [merged]
    return clusters


def prep(c):
    c["hex"] = "#%02x%02x%02x" % tuple(int(math.floor(v + 0.5)) for v in np.clip(c["rgb"], 0, 255))
    c["chroma"] = float(math.hypot(c["lab"][1], c["lab"][2]))


def finalize(clusters, total):
    """Частки відносно total, hex, chroma; відкидаємо шум, сортуємо за часткою."""
    for c in clusters:
        c["share"] = c["w"] / total
        prep(c)
    clusters = [c for c in clusters if c["share"] >= MIN_SHARE_OUT]
    clusters.sort(key=lambda c: -c["share"])
    return clusters


def residual(rl, rw, rm):
    """Кластеризація залишку; частки відносно суми залишку (renormalize)."""
    if rw.size == 0:
        return []
    return finalize(cluster_bins(rl, rw, rm), float(rw.sum()))


def _item(c, share, role, removed=False):
    d = {"hex": c["hex"], "share": round(float(share), 4), "role": role}
    if removed:
        d["removed"] = True
    return d


def assign_roles(rest, bg, surface_taken=False):
    """Ролі для кластерів rest (фон bg — лише як еталон для відстаней і контрасту)."""
    for c in rest:
        c["role"] = "other"

    def de_bg(c):
        return float(np.linalg.norm(c["lab"] - bg["lab"]))

    def free(xs):
        return [c for c in xs if c["role"] == "other"]

    # surface: найближчий до фону з часткою > 5 % (якщо ще не вилучена як поверхня)
    if not surface_taken:
        cands = [c for c in rest if c["share"] > SURFACE_MIN_SHARE]
        if cands:
            min(cands, key=de_bg)["role"] = "surface"
    rest = free(rest)

    # accent: найнасиченіший з часткою від 1 %; верхньої межі немає (лайм у світлому спортивному UI — accent)
    cands = [c for c in rest if c["share"] >= ACCENT_MIN_SHARE and c["chroma"] >= ACCENT_MIN_CHROMA]
    if cands:
        max(cands, key=lambda c: c["chroma"])["role"] = "accent"
    rest = free(rest)

    # текст: мала площа, не майже-фон; переважно AA-контраст і нейтральні
    def text_ok(c):
        return TEXT_MIN_SHARE <= c["share"] <= TEXT_MAX_SHARE and de_bg(c) >= TEXT_MIN_DE_BG

    def neutral_first(pool):
        aa = [c for c in pool if contrast_ratio(c["rgb"], bg["rgb"]) >= 4.5]
        pool = aa if aa else pool
        neutral = [c for c in pool if c["chroma"] <= TEXT_NEUTRAL_CHROMA]
        return neutral if neutral else pool

    # textPrimary: найбільший контраст до фону
    cands = neutral_first([c for c in rest if text_ok(c)])
    tp = None
    if cands:
        tp = max(cands, key=lambda c: contrast_ratio(c["rgb"], bg["rgb"]))
        tp["role"] = "textPrimary"
    rest = free(rest)

    # textSecondary: яскравість між фоном і textPrimary, ближча до середини
    if tp is not None:
        Lbg, Lt = bg["lab"][0], tp["lab"][0]
        lo, hi = min(Lbg, Lt), max(Lbg, Lt)
        mid = (Lbg + Lt) / 2.0
        cands = neutral_first([c for c in rest if text_ok(c) and lo < c["lab"][0] < hi])
        if cands:
            min(cands, key=lambda c: abs(c["lab"][0] - mid))["role"] = "textSecondary"


def extract_from_array(arr, opts=None):
    """arr: (H, W, 4) uint8 RGBA. Повертає список {hex, share, role[, removed]}."""
    opts = opts or {}
    b = build_bins(arr, opts)
    if b is None:
        return []
    lab, w, means = b
    total_all = float(w.sum())
    clusters = finalize(cluster_bins(lab, w, means), total_all)
    if not clusters:
        return []
    bg = clusters[0]

    # звичайний режим: фон <= 50 % або видалення вимкнено (абсолютні пороги, як раніше)
    if not opts.get("remove_bg", True) or bg["share"] <= REMOVE_BG_SHARE:
        bg["role"] = "background"
        assign_roles(clusters[1:], bg)
        return [_item(c, c["share"], c["role"]) for c in clusters]

    # режим «фон окремо»: прибираємо фон, рахуємо решту (частки відносно залишку)
    removed = [(bg, "background")]
    keep = np.linalg.norm(lab - bg["lab"], axis=1) > REMOVE_DE
    rl, rw, rm = lab[keep], w[keep], means[keep]
    rest = residual(rl, rw, rm)
    surface_taken = False
    if rest and rest[0]["share"] > REMOVE_SURFACE_SHARE and len(removed) < MAX_PASSES:
        sf = rest[0]
        removed.append((sf, "surface"))
        surface_taken = True
        keep2 = np.linalg.norm(rl - sf["lab"], axis=1) > REMOVE_DE
        rl, rw, rm = rl[keep2], rw[keep2], rm[keep2]
        rest = residual(rl, rw, rm)

    assign_roles(rest, bg, surface_taken=surface_taken)
    out = [_item(c, c["w"] / total_all, role, removed=True) for c, role in removed]
    out += [_item(c, c["share"], c["role"]) for c in rest]
    return out


def extract_file(path, rect=None, opts=None):
    return extract_from_array(load_rgba(path, rect), opts)


def list_refs(ref_dir=None):
    """{ref_NN: шлях до файлу}; відповідність беруть з refs.local.json (див. refs_util.py)."""
    return load_ref_map(ref_dir or REF_DIR)


def _draw_strip(d, x0, y, pal, font, small, show_removed_note=True):
    """Смуга: видалені елементи — частка від усієї картинки; залишок — відносна частка * (1 - видалені)."""
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


def make_preview(before, after, out_path):
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
    d.text((12, 10), "Палітри: до (v2) і після (v3, фон окремо). Смуга: частка від усієї картинки; "
                     "у легенді для залишку — частка від залишку.", fill=(20, 20, 20), font=font)
    y = 40
    for name in names:
        bg = next((c["hex"] for c in after[name] if c["role"] == "background"), "#cccccc")
        d.rectangle([12, y + 4, 12 + THUMB_W, y + 4 + THUMB_H], fill=bg, outline=(200, 200, 200))  # лише колір фону, без картинки
        d.text((12, y + THUMB_H + 8), name, fill=(60, 60, 60), font=small)
        # до
        d.text((BAR_X, y), "до (v2)", fill=(90, 90, 90), font=small)
        _draw_strip(d, BAR_X, y + 16, before.get(name, []), font, small)
        _legend(d, BAR_X, y + 64, before.get(name, []), small)
        # після
        d.text((BAR_X, y + 100), "після (v3)", fill=(90, 90, 90), font=small)
        _draw_strip(d, BAR_X, y + 116, after[name], font, small)
        _legend(d, BAR_X, y + 164, after[name], small)
        y += ROW_H
    img.save(out_path)
    return out_path


def run_all(opts, rect):
    return {rid: extract_file(p, rect, opts) for rid, p in list_refs().items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "palettes.json"))
    ap.add_argument("--preview", default=os.path.join(HERE, "palettes_preview.png"))
    ap.add_argument("--before", help="JSON з попередньою версією для колонки «до» у превʼю")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--no-remove-bg", action="store_true", help="вимкнути режим «фон окремо»")
    ap.add_argument("--no-margins", action="store_true", help="не відсіювати білі поля")
    ap.add_argument("--center", action="store_true", help="зважити центр")
    ap.add_argument("--edge-only", action="store_true", help="ігнорувати кольори лише на межі")
    ap.add_argument("--rect", help="x,y,w,h у відсотках")
    ap.add_argument("--dump-bins", metavar="DIR", help="зберегти зменшені RGBA (.bin + .json) для тесту JS")
    a = ap.parse_args()

    rect = [float(v) for v in a.rect.split(",")] if a.rect else None
    opts = {"margins": not a.no_margins, "center": a.center, "edge_only": a.edge_only,
            "remove_bg": not a.no_remove_bg}
    results = run_all(opts, rect)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("записано:", a.out, len(results), "референсів")
    for name, pal in results.items():
        print("%-40s %s" % (name[:40], " ".join(
            "%s%s:%s:%.3f" % (c["role"][:4], "*" if c.get("removed") else "", c["hex"], c["share"]) for c in pal)))
    if not a.no_preview:
        before = {}
        if a.before and os.path.exists(a.before):
            before = json.load(open(a.before, encoding="utf-8"))
        make_preview(before, results, a.preview)
        print("превʼю:", a.preview)
    if a.dump_bins:
        os.makedirs(a.dump_bins, exist_ok=True)
        for rid, path in list_refs().items():
            arr = load_rgba(path, rect)
            base = rid
            arr.tofile(os.path.join(a.dump_bins, base + ".bin"))
            variants = {
                "default": extract_from_array(arr, {"remove_bg": True}),
                "noRemoveBg": extract_from_array(arr, {"remove_bg": False}),
                "center": extract_from_array(arr, {"center": True}),
                "edgeOnly": extract_from_array(arr, {"edge_only": True}),
                "noMargins": extract_from_array(arr, {"margins": False}),
                "rect": extract_from_array(crop_arr(arr, [10, 5, 60, 70]), {}),
            }
            with open(os.path.join(a.dump_bins, base + ".json"), "w") as f:
                json.dump({"width": int(arr.shape[1]), "height": int(arr.shape[0]),
                           "rect": [10, 5, 60, 70], "variants": variants}, f)
        print("дампи:", a.dump_bins)


if __name__ == "__main__":
    main()
