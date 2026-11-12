"""Оцінка палітр проти еталонної розмітки (ground_truth.json).

Запуск: python3 evaluate.py [palettes.json ...]
Метрики по ролях (background, surface, accent, textPrimary):
  role  = у палітрі є колір цієї ролі в межах ΔE76 <= 10 від будь-якої з допустимих альтернатив
  exact = те саме, але ΔE76 <= 4 (майже точний hex)
  any   = колір знайдено в палітрі під будь-якою роллю (ΔE76 <= 10): колір видно, роль може бути хибною
Еталон чернетковий: перевіряйте ground_truth_check.png.
"""
import json, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES = ["background", "surface", "accent", "textPrimary"]


def lab(hexv):
    h = hexv.lstrip("#")
    rgb = np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], float) / 255
    rgb = np.where(rgb > 0.04045, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = M @ rgb / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def de(a, b):
    return float(np.linalg.norm(lab(a) - lab(b)))


def evaluate(pal_path, gt):
    pal = json.load(open(pal_path))
    stats = {r: {"n": 0, "role": 0, "exact": 0, "any": 0} for r in ROLES}
    detail = []
    for name, g in gt.items():
        colors = pal.get(name, [])
        for role, alts in g["roles"].items():
            s = stats[role]
            s["n"] += 1
            best = {"role": 999, "any": 999}
            for a in alts:
                for c in colors:
                    d = de(a["hex"], c["hex"])
                    best["any"] = min(best["any"], d)
                    if c["role"] == role:
                        best["role"] = min(best["role"], d)
            s["role"] += best["role"] <= 10
            s["exact"] += best["role"] <= 4
            s["any"] += best["any"] <= 10
            detail.append((name, role, best["role"], best["any"]))
    return stats, detail


def main():
    gt = json.load(open(os.path.join(HERE, "ground_truth.json")))
    paths = sys.argv[1:] or [os.path.join(HERE, "palettes.json")]
    for p in paths:
        stats, detail = evaluate(p, gt)
        print(f"\n== {os.path.basename(p)} ==")
        print(f"{'роль':12s} {'n':>3s} {'role':>6s} {'exact':>6s} {'any':>6s}")
        for r in ROLES:
            s = stats[r]
            print(f"{r:12s} {s['n']:3d} {s['role']:6d} {s['exact']:6d} {s['any']:6d}")
        miss = [(n[:30], r, round(b, 1), round(a, 1)) for n, r, b, a in detail if b > 10]
        print("промахи по ролі (референс, роль, мін ΔE за роллю, мін ΔE будь-яка роль):")
        for m in miss:
            print("  ", m)


if __name__ == "__main__":
    main()
