// Перевірка паритету JS (extract-colors.js) з Python (extract_palettes.py).
// Запуск: node test_node.js <папка_дампів>
// Вибірка: ref_05, ref_06, ref_07, ref_09, ref_10, ref_11 (імена файлів дампів = ідентифікатори).
// Дампи створює: python3 extract_palettes.py --dump-bins <папка_дампів>
const fs = require('fs');
const path = require('path');
const CE = require('./extract-colors.js');

const dir = process.argv[2];
if (!dir) { console.error('usage: node test_node.js <dump_dir>'); process.exit(1); }

const jsons = fs.readdirSync(dir).filter(f => f.endsWith('.json')).sort();
const pick = jsons.filter(f => /^(ref_05|ref_06|ref_07|ref_09|ref_10|ref_11)\./.test(f));

function rgbaFrom(file, meta) {
  const buf = fs.readFileSync(file.replace(/\.json$/, '.bin'));
  return { data: new Uint8ClampedArray(buf.buffer, buf.byteOffset, buf.length), width: meta.width, height: meta.height };
}

function hexToRgb(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }

let allRolesOk = true, maxHexDiff = 0, sameCount = 0, total = 0;
const rows = [];
for (const j of pick) {
  const full = path.join(dir, j);
  const meta = JSON.parse(fs.readFileSync(full, 'utf8'));
  const img = rgbaFrom(full, meta);
  const runs = {
    default: () => CE.extractPalette(img, {}),
    noRemoveBg: () => CE.extractPalette(img, { removeBackground: false }),
    center: () => CE.extractPalette(img, { center: true }),
    edgeOnly: () => CE.extractPalette(img, { edgeOnly: true }),
    noMargins: () => CE.extractPalette(img, { margins: false }),
    rect: () => CE.extractPalette(img, { rect: { x: meta.rect[0], y: meta.rect[1], w: meta.rect[2], h: meta.rect[3] } }),
  };
  for (const [variant, fn] of Object.entries(runs)) {
    const js = fn();
    const py = meta.variants[variant];
    total++;
    const name = j.replace(/\.json$/, '') + ' [' + variant + ']';
    const countOk = js.length === py.length;
    let rolesOk = countOk, hd = 0;
    if (countOk) {
      js.forEach((c, i) => {
        if (c.role !== py[i].role) rolesOk = false;
        const a = hexToRgb(c.hex), b = hexToRgb(py[i].hex);
        for (let k = 0; k < 3; k++) hd = Math.max(hd, Math.abs(a[k] - b[k]));
      });
    }
    if (countOk) sameCount++;
    if (!rolesOk) allRolesOk = false;
    maxHexDiff = Math.max(maxHexDiff, hd);
    rows.push({ name, countOk, rolesOk, hd, js, py });
  }
}

for (const r of rows) {
  const fmt = arr => arr.map(c => `${c.role.slice(0, 4)}:${c.hex}:${(c.share * 100).toFixed(1)}`).join(' ');
  console.log(`${r.name}\n  JS: ${fmt(r.js)}\n  PY: ${fmt(r.py)}\n  кількість=${r.countOk ? 'ok' : 'різна'} ролі=${r.rolesOk ? 'ok' : 'різні'} max|Δhex|=${r.hd}`);
}
console.log(`\nВсього прогонів: ${total}; однакова кількість кольорів: ${sameCount}; ролі збіглися у всіх: ${allRolesOk}; max Δ каналу hex: ${maxHexDiff}`);
process.exitCode = allRolesOk && sameCount === total ? 0 : 2;
