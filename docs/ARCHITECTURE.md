# Архітектура ref-board.html

Один HTML-файл (CSS у `<style>`, JavaScript в IIFE з `'use strict'`), без збірки і npm. Єдина зовнішня залежність — `color-test/extract-colors.js`, що підключається звичайним `<script>` і дає глобал `ColorExtract`. Назовні нічого не експортується.

## Розкладка

| Зона | Елемент | Призначення |
|---|---|---|
| `aside.left` | `#folderBar`, `#list`, `.foot` | папка, список референсів, експорт і імпорт |
| `main.work` (вертикальний скрол) | `.toolbar` + `#vp` > `#stage` (`img` + `#ov`) | режими «Виділити / Піпетка / Перегляд», масштаб, картинка з виділеннями |
| | `#metaRow` | кількість тез, оцінка N з 6, шрифт, джерело, теги |
| | `#tabs`, `#panel` | «Тези по областях», «Загальна оцінка», «Колір» |

CSS-grid `280px | 1fr`, мінімальна ширина 900 px. Токени в `:root` (нейтральна світла тема).

## Логічні блоки коду

1. **Довідники і чисті функції** (тестуються самотестом): `CATS`, `VERDICTS`, `CARDS`, `ROLES`; `nextVersion`, `exportName`, `cropFileName`, `cropBox`; `contrast`, `grade` (WCAG); `validateRect`, `normalizeHighlight`, `normalizeRef`, `normalizeImport`; `statusOfRef`; `parseKey`, `matchOrphans`, `relinkInRefs`.
2. **Сховище**: `loadStore`/`saveNow`/`saveSoon` (`localStorage`, запис із дебаунсом 300 мс і примусовим збереженням при закритті).
3. **Файли**: `S.files` (`Map` ключ -> `{key, name, file, src: 'folder'|'added', url}`), мініатюри (`makeThumb`, черга по 2, кеш `S.thumbs`), `scanFolder`, `addFilesList`, `computeSuggestions`.
4. **IndexedDB**: одне кешоване зʼєднання (`idbOpen`), лише дескриптор папки.
5. **Експорт/імпорт**: `exportJson(withCrops)`, `writeCrops`, `listExports`, `createExportFile`, `importJson`, `fallbackExport`.
6. **Виділення**: `onDown/onMove/onUp/onCancel`, `startPan`; undo (`pushUndo`, `doUndo`).
7. **Колір**: `getFull` (повне зображення на offscreen canvas), `pickColorAt`, `runExtract`, `extractAsync` (Web Worker, запасний шлях у головному потоці), `applyPalette`, `contrastPairs`.
8. **Рендер**: список оновлюється точково (`updateItem`, збереження DOM-елементів), решта через `renderCenter/Meta/Tabs/Panel`; картка оцінки перемальовується окремо (`refreshCard`).
9. **Тестові режими**: `runSelftest`, `setupDemo`.

## Стан у пам'яті (`S`)

`supported`, `handle`, `permOk`, `writeOk`; `files`, `thumbs`, `suggest` (oldKey -> збіг); `cur`, `tab`, `mode` (`draw|pick|pan`), `sel`; `draft`, `drag`, `undo`; `scale`, `fit`, `natW`, `natH`; `busy` (витягування кольору), `full` (повне зображення для піпетки і кольору). Картинка відкритого референсу отримує `objectURL` лише при відкритті й звільняється при переході.

## Сховища

| Сховище | Ключ | Що |
|---|---|---|
| `localStorage` | `ref-board:v1` (зміст version 2) | `{version, refs, scanIds, newIds, folderName}` |
| `localStorage` | `ref-board:v1:backup-v1` | одноразовий резерв даних v1 перед міграцією |
| `localStorage` | `ref-board:test` | дані режимів `?selftest=1` і `?demo=1` (реальні дані не чіпаються) |
| IndexedDB | база `ref-board`, сховище `kv`, ключ `dirHandle` | дескриптор папки |
| File System Access API | вибрана папка | картинки, файли експорту, каталоги кропів |

## Ключ файлу і привʼязка

Ключ `імʼя|розмір|lastModified` чутливий до перезбереження файлу. Щоб тези не осиротіли: після сканування запис без файлу зіставляється з файлом без даних за `імʼя+розмір`, далі за `імʼя` (`matchOrphans`). У списку такий запис має кнопку «Привʼязати до файлу»; діалог пропонує рекомендований збіг і решту файлів (`doRelink` переносить запис на новий ключ і оновлює `scanIds/newIds`).

## Сканування папки і «нове»

1. `scanFolder` читає файли з `IMG_RE`.
2. З `S.files` видаляються лише записи `src: 'folder'`, яких немає в папці; додані вручну (`src: 'added'`) лишаються.
3. «Нове» = ключі, відсутні в попередньому скані (`store.scanIds`) тієї ж папки. Папка порівнюється через `FileSystemHandle.isSameEntry` (фолбек на імʼя); при зміні папки `scanIds` і `newIds` скидаються.
4. Мітка зникає при відкритті референсу.

## Витягування кольору

`ColorExtract.imageDataFromSource(file, 1600)` (вся картинка) або `getImageData` повного canvas (виділення; `opts.rect` у відсотках, `hiRes` для малих ділянок) -> `extractAsync`. Воркер створюється з Blob-коду, який робить `importScripts` скрипта бібліотеки; якщо воркер недоступний або повільніший за 30 с, рахується в головному потоці. Під час розрахунку кнопки неактивні, показано «Рахую…». `removeBackground` лишається за замовчуванням бібліотеки.

## Експорт

1. Якщо запису немає, а папка обрана, спершу `requestWrite()` (жест користувача).
2. `listExports` + `nextVersion` -> `createExportFile` (перший вільний `ref-board_vNNN_...json`, наявні не перезаписує).
3. «Експорт для аналізу»: `writeCrops` створює `ref-board_vNNN_crops/`, вирізає PNG з повного зображення (`cropBox`), у JSON виділенню додається `crop`. Для референсів без файлу в поточному списку `crop: null`.
4. Помилка запису: порожній файл видаляється, JSON іде в Downloads (`ref-board.json`) без кропів, тост пояснює причину.

## Імпорт

`normalizeImport` приймає `version` 1 і 2, мігрує, відкидає некоректні виділення й записи без `id`, показує кількість відкинутого. Імʼя `Ref Board.json` не відповідає `EXPORT_RE`, тож імпортується через «Імпорт JSON», а не зі списку папки.

## Події

Делегування на контейнерах (`#list`, `#panel`, `#metaRow`, `#modeSeg`). Введення тексту пише в дані одразу і зберігає з дебаунсом; зміна вердиктів/чіпсів перемальовує лише потрібну картку або панель. Клавіші: Delete/Backspace видаляє вибране виділення, Ctrl/Cmd+Z скасовує, Escape знімає вибір. Вказівник: `pointermove/up/cancel` на `window`, `touch-action: none` на оверлеї.

## Перенос в інший проєкт

Достатньо скопіювати `ref-board.html` і `color-test/extract-colors.js`. Для співіснування кількох інструментів на одному origin змініть ключі (`ref-board:v1`, база `ref-board`).
