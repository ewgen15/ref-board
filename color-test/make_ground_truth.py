"""Чернетка еталонної розмітки ролей кольорів.

Для кожного референсу задано точки (x%, y%) на картинці для ролей. Скрипт бере з ПОВНОЇ картинки
типовий (медіанний) колір навколо точки і пише ground_truth.json. Ролі: background, surface, accent,
textPrimary. Для кожної ролі може бути кілька допустимих альтернатив (будь-яка збігається = влучення).
Також створює ground_truth_check.png: НЕЙТРАЛЬНИЙ лист для перевірки: порожня рамка з пропорцією зображення,
позначки точок (у відсотках) з підписами ролей і плитки hex. Пікселі референсів у PNG не потрапляють.
Ключі: ref_01, ref_02... (відповідність імен файлів у локальному refs.local.json).

Запуск: python3 make_ground_truth.py [--ref-dir ПАПКА]  (потрібні картинки, лише щоб обчислити кольори)
        python3 make_ground_truth.py --gradients-sheet  (лише лист ground_truth_gradients_check.png з JSON розмітки)
Чернетку ТРЕБА перевірити людиною (див. ground_truth_check.png) і виправити точки в POINTS нижче.
"""
import argparse, json, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from refs_util import default_ref_dir, load_ref_map  # noqa: E402

REF = default_ref_dir()  # папка з референсами (параметр --ref-dir, див. refs_util.py)

# mode: median (типовий колір вікна), dark / light (найтемніший / найсвітліший 8 % вікна: для штрихів тексту)
# кожна точка: (x%, y%, radius%, mode)
def P(x, y, r=0.6, mode="median"):
    return (x, y, r, mode)

POINTS = {
  "ref_01": {
    "background": [P(1.5, 2)],
    "surface": [P(26, 7), P(87, 70)],
    "accent": [P(42.5, 89.8)],
  },
  "ref_02": {
    "background": [P(3, 30)],
    "surface": [P(44, 60)],
    "accent": [P(84, 24)],
  },
  "ref_03": {
    "background": [P(27.5, 50)],
    "surface": [P(46, 63)],
    "accent": [P(34.5, 61.5)],
    "textPrimary": [P(37.3, 24, 3, "dark")],
  },
  "ref_04": {
    "background": [P(60, 10)],
    "surface": [P(22.5, 53)],
    "accent": [P(94, 71), P(50, 2)],
  },
  "ref_05": {
    "background": [P(3, 8)],
    "surface": [P(86.9, 32.4)],
    "accent": [P(58.5, 48.1)],
    "textPrimary": [P(13, 33, 3, "dark")],
  },
  "ref_06": {
    "background": [P(3, 5)],
    "surface": [P(78.6, 60), P(13.6, 57)],
    "accent": [P(88.6, 29)],
    "textPrimary": [P(15.5, 27.3, 5, "dark")],
  },
  "ref_07": {
    "background": [P(74.5, 33)],
    "surface": [P(60, 40)],
    "accent": [P(3, 45), P(75, 87), P(54.5, 12.5)],
  },
  "ref_08": {
    # фон = градієнт неба: верх (#79b1cd) і низ (#fdf3e6) — обидва допустимі
    "background": [P(10, 8), P(50, 97)],
    "surface": [P(25, 72.7)],
    "accent": [P(79.4, 44.2), P(21.4, 56.7), P(50, 21.5)],
    "textPrimary": [P(73, 54.8, 3, "dark")],
  },
  "ref_09": {
    "background": [P(5, 10)],
    "surface": [P(70, 22)],
    "accent": [P(31, 86)],
  },
  "ref_10": {
    "background": [P(2, 50), P(88, 40)],
    "surface": [P(71, 76), P(88, 40)],
    "accent": [P(60, 47.7)],
  },
  "ref_11": {
    "background": [P(72, 28), P(2, 12)],
    "surface": [P(50, 73)],
    "accent": [P(45, 91)],
    "textPrimary": [P(67, 43.6, 4, "light")],
  },
  "ref_12": {
    "background": [P(50, 60), P(50, 5)],
    "surface": [P(15, 74)],
    "accent": [P(20.5, 45)],
  },
  "ref_13": {
    "background": [P(30, 54.3)],
    "surface": [P(21.5, 62.7)],
    "accent": [P(45, 34.5)],
  },
}


def sample(img_arr, x, y, r, mode):
    H, W, _ = img_arr.shape
    cx, cy = x / 100 * W, y / 100 * H
    rr = max(3, int(r / 100 * W))
    x0, x1 = max(0, int(cx - rr)), min(W, int(cx + rr))
    y0, y1 = max(0, int(cy - rr)), min(H, int(cy + rr))
    px = img_arr[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    if mode == "median":
        c = np.median(px, axis=0)
    else:
        lum = px @ np.array([0.299, 0.587, 0.114])
        k = max(1, int(len(px) * 0.08))
        idx = np.argsort(lum)[:k] if mode == "dark" else np.argsort(lum)[-k:]
        c = np.median(px[idx], axis=0)
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in c)


ROLE_COL = {"background": (255, 0, 255), "surface": (0, 160, 255), "accent": (255, 120, 0), "textPrimary": (0, 200, 0)}
TW = 420  # ширина рамки
SIDE = 360  # ширина колонки з плитками


def _font(size):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        return ImageFont.load_default()


def _frame(ratio):
    """Порожня рамка з пропорцією зображення (без пікселів референсу)."""
    h = max(60, int(TW * ratio))
    t = Image.new("RGB", (TW, h), (236, 236, 236))
    ImageDraw.Draw(t).rectangle([0, 0, TW - 1, h - 1], outline=(150, 150, 150))
    return t


def _sheet(cells):
    """cells: список листів однакової ширини, розкладаються у 2 колонки."""
    colw, cols = TW + SIDE, 2
    heights = [max(r.height for r in cells[i:i + cols]) for i in range(0, len(cells), cols)]
    canvas = Image.new("RGB", (colw * cols, sum(heights) + 4 * len(heights)), (240, 240, 240))
    y = 0
    for ri, h in enumerate(heights):
        for ci in range(cols):
            k = ri * cols + ci
            if k < len(cells):
                canvas.paste(cells[k], (ci * colw, y))
        y += h + 4
    return canvas


def image_ratio(ref_map, rid):
    with Image.open(ref_map[rid]) as im:
        return im.height / im.width


def check_sheet(ref_map, gt, out):
    """Нейтральний лист для ручної перевірки еталона ролей."""
    f = _font(12)
    cells = []
    for rid, g in gt.items():
        t = _frame(image_ratio(ref_map, rid))
        d = ImageDraw.Draw(t)
        for role, items in g["roles"].items():
            for i, it in enumerate(items):
                X, Y = it["at"][0] / 100 * t.width, it["at"][1] / 100 * t.height
                col = ROLE_COL[role]
                d.ellipse([X - 6, Y - 6, X + 6, Y + 6], outline=col, width=3)
                lab = "%s%d (%g%%, %g%%)" % (role[:2], i, it["at"][0], it["at"][1])
                tx = X + 9 if X < TW * 0.6 else X - 9 - d.textlength(lab, font=f)
                d.text((tx, Y - 6), lab, fill=col, font=f)
        sheet = Image.new("RGB", (TW + SIDE, max(t.height, 4 * 32 + 24)), (255, 255, 255))
        sheet.paste(t, (0, 0))
        d2 = ImageDraw.Draw(sheet)
        d2.text((TW + 6, 2), rid, fill=(0, 0, 0), font=_font(14))
        y = 22
        for role, items in g["roles"].items():
            for it in items:
                d2.rectangle([TW + 6, y, TW + 46, y + 28], fill=it["hex"], outline=(120, 120, 120))
                d2.text((TW + 52, y + 8), "%s %s" % (role, it["hex"]), fill=(0, 0, 0), font=f)
                y += 32
        cells.append(sheet)
    _sheet(cells).save(out)


def gradients_sheet(ref_map, gt, out):
    """Нейтральний лист розмітки градієнтів: рамка з пропорцією, прямокутники у відсотках, плитки кінців."""
    f = _font(12)
    cells = []
    for rid, g in gt.items():
        t = _frame(image_ratio(ref_map, rid))
        d = ImageDraw.Draw(t)

        def box(r, col, label):
            x0, y0 = r["x"] / 100 * t.width, r["y"] / 100 * t.height
            x1, y1 = (r["x"] + r["w"]) / 100 * t.width, (r["y"] + r["h"]) / 100 * t.height
            d.rectangle([x0, y0, x1, y1], outline=col, width=2)
            d.text((x0 + 3, y0 + 2), label, fill=col, font=f)

        tiles = []
        for i, r in enumerate(g.get("regions", [])):
            box(r["rect"], (30, 90, 220), "%d %s" % (i, r["type"]))
            tiles.append(("%d %s%s" % (i, r["type"], " (фон)" if r.get("is_background") else ""), r["ends"]))
        for i, r in enumerate(g.get("not_gradient", [])):
            box(r["rect"], (220, 30, 30), "ng%d" % i)
        sheet = Image.new("RGB", (TW + SIDE, max(t.height, len(tiles) * 32 + 40)), (255, 255, 255))
        sheet.paste(t, (0, 0))
        d2 = ImageDraw.Draw(sheet)
        d2.text((TW + 6, 2), "%s  gradient_background=%s" % (rid, g.get("gradient_background")), fill=(0, 0, 0), font=f)
        y = 22
        for label, ends in tiles:
            for k, hx in enumerate(ends):
                d2.rectangle([TW + 6 + k * 44, y, TW + 46 + k * 44, y + 28], fill=hx, outline=(120, 120, 120))
            d2.text((TW + 100, y + 8), "%s %s" % (label, " -> ".join(ends)), fill=(0, 0, 0), font=f)
            y += 32
        cells.append(sheet)
    _sheet(cells).save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-dir", help="папка з референсами (інакше REF_BOARD_REFS, поле _dir у refs.local.json або ./refs)")
    ap.add_argument("--gradients-sheet", action="store_true",
                    help="лише створити ground_truth_gradients_check.png з ground_truth_gradients.json")
    a = ap.parse_args()
    ref_map = load_ref_map(a.ref_dir or REF)
    if a.gradients_sheet:
        gg = json.load(open(os.path.join(HERE, "ground_truth_gradients.json"), encoding="utf-8"))
        gradients_sheet(ref_map, gg, os.path.join(HERE, "ground_truth_gradients_check.png"))
        print("ok", len(gg), "референсів")
        return
    gt = {}
    for rid, roles in POINTS.items():
        with Image.open(ref_map[rid]) as im0:
            arr = np.asarray(im0.convert("RGB"))
        gt[rid] = {"status": "draft", "roles": {}}
        for role, pts in roles.items():
            gt[rid]["roles"][role] = [
                {"hex": sample(arr, *p[:2], p[2], p[3]), "at": [p[0], p[1]], "mode": p[3]} for p in pts
            ]
    json.dump(gt, open(os.path.join(HERE, "ground_truth.json"), "w"), ensure_ascii=False, indent=2)
    check_sheet(ref_map, gt, os.path.join(HERE, "ground_truth_check.png"))
    print("ok", len(gt), "референсів")


if __name__ == "__main__":
    main()
