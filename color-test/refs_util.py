"""Нейтральні ідентифікатори референсів (ref_01, ref_02, ...).

Відповідність «ідентифікатор -> імʼя файлу» лежить у локальному refs.local.json
(формат {"ref_01": "<імʼя файлу в папці з референсами>", ...}); файл у репозиторій не потрапляє.
Якщо refs.local.json немає, береться відсортований за імʼям список *.webp і *.png у папці
(ref_01 = перший файл) і друкується попередження.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_MAP = os.path.join(HERE, "refs.local.json")


def load_ref_map(ref_dir):
    """Повертає {ref_id: шлях до файлу} у порядку ідентифікаторів."""
    if os.path.exists(LOCAL_MAP):
        with open(LOCAL_MAP, encoding="utf-8") as f:
            m = json.load(f)
        return {k: os.path.join(ref_dir, v) for k, v in sorted(m.items()) if not k.startswith("_")}
    files = sorted(glob.glob(os.path.join(ref_dir, "*.webp")) + glob.glob(os.path.join(ref_dir, "*.png")))
    print("УВАГА: refs.local.json не знайдено; ідентифікатори ref_NN призначено за відсортованим "
          "порядком файлів у %s (%d файлів)." % (ref_dir, len(files)), file=sys.stderr)
    return {"ref_%02d" % (i + 1): p for i, p in enumerate(files)}


def default_ref_dir():
    """Папка з референсами: змінна середовища REF_BOARD_REFS, інакше поле "_dir" у refs.local.json
    (відносно color-test/ або абсолютне), інакше ./refs поруч зі скриптами."""
    env = os.environ.get("REF_BOARD_REFS")
    if env:
        return os.path.abspath(env)
    if os.path.exists(LOCAL_MAP):
        with open(LOCAL_MAP, encoding="utf-8") as f:
            d = json.load(f).get("_dir")
        if d:
            return os.path.normpath(os.path.join(HERE, d))
    return os.path.join(HERE, "refs")
