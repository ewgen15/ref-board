"""Підсумок розмітки градієнтів (ground_truth_gradients.json). Детектора ще немає: оцінки немає.

Запуск: python3 evaluate_gradients.py
Друкує: кількість референсів з gradient_background, кількість градієнтних областей (за типом),
кількість областей not_gradient (гладкі, але не градієнти), статуси розмітки.
"""
import json, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    gt = json.load(open(os.path.join(HERE, "ground_truth_gradients.json"), encoding="utf-8"))
    n_bg = [k for k, v in gt.items() if v.get("gradient_background")]
    regions = [r for v in gt.values() for r in v.get("regions", [])]
    types = Counter(r["type"] for r in regions)
    n_ng = sum(len(v.get("not_gradient", [])) for v in gt.values())
    statuses = Counter(v.get("status", "?") for v in gt.values())
    print(f"референсів у розмітці: {len(gt)}; статуси: {dict(statuses)}")
    print(f"gradient_background = true: {len(n_bg)}")
    for k in n_bg:
        print(f"  - {k}")
    print(f"градієнтних областей: {len(regions)} (тип: " +
          ", ".join(f"{t} {c}" for t, c in sorted(types.items())) + ")")
    print(f"областей not_gradient (гладкі, але не градієнт): {n_ng}")
    per = [(k[:34], len(v.get("regions", [])), len(v.get("not_gradient", []))) for k, v in gt.items()]
    print(f"{'референс':34s} {'град.':>6s} {'не град.':>9s}")
    for name, a, b in per:
        print(f"{name:34s} {a:6d} {b:9d}")


if __name__ == "__main__":
    main()
