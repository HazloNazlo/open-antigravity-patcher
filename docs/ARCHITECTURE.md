# Архитектура патчера

Документ описывает, как устроен код в `source/patcher/` после доработки 1.4.0.
Исходная архитектура AvenCores сохранена (TUI, меню, модуль-разбиение), изменена
логика патчей.

---

## 1. Слои

```
┌─────────────────────────────────────────────────────────────┐
│  source/main.py                                              │
│  elevation (sudo / UAC) → запуск TUI, обработка ошибок      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│  patcher/cli.py  — TUI                                       │
│  • print_target_info()  панель статуса 5 целей              │
│  • run_cli()           меню 1..14                           │
│  • offer_download_and_block() / confirmed() / pause()       │
└───────┬───────────────┬───────────────┬──────────────┬───────┘
        │               │               │              │
        ▼               ▼               ▼              ▼
  ide/patcher    manager/patcher   agy/patcher   vscode/patcher
        │               │               │              │
        │               └───────┬───────┘              │
        │                       ▼                      │
        │              utils/binary.py ──────┐         │
        ▼                                  ▼         ▼
  gates.py ◄───────────────────────────────┘   (свои паттерны,
   ▲ сигнатуры + план + применение                без изменений)
   │
   ▼
 utils/file.py  — запись, staging/swap, переподпись, бэкапы
```

---

## 2. Ключевое решение: один движок

До правки сигнатуры гейтов жили в трёх независимых реализациях
(`manager/patcher.py`, `agy/patcher.py`, `ide/patcher.py`). Они разошлись: AvenCores
патчил в `agy` только экран CLI, а валидатор `hasValidAuth` не трогал вообще —
поэтому `~/.local/bin/agy` после правок AvenCores оставался с закрытым экраном,
но открытым валидатором (и с разорванной подписью).

Сейчас:

* **`patcher/gates.py`** — единственное место, где описаны сигнатуры, план правок и
  применение. Не содержит I/O.
* **`patcher/utils/binary.py`** — единственное место, где применяется байтовый патч
  к бинарнику (с учётом App Management и бэкапов).

Модули `manager/` и `agy/` — тонкие обёртки: discovery → version check → вызов движка →
вывод результата.

---

## 3. Поток данных патча бинарника

```
do_patch_manager(path) / do_patch_agy(path)
        │
        ├─ discovery: файл найден?
        ├─ version check (менеджер — по app.asar)
        ├─ is_locked? (только Windows)
        │
        ▼
utils/binary.py: patch_binary(path, cli_screens, revert_proxy)
        │
        ├─ read_binary(path)
        ├─ gates.plan_go_patches(data, ...)
        │     ├─ VALIDATOR_GATES  → hasValidAuth
        │     ├─ CLI_GATES        → экраны допустимости (только для agy)
        │     └─ find_proxy_reverts() → откат AG_LS_PROXY
        │     возвращает (edits, states)
        │
        ├─ edits пусто? → ok=True, "already patched"  (идемпотентность)
        │
        ├─ gates.apply_edits(data, edits)
        │     • правки с конца к началу
        │     • проверка: байты на месте == what планировщик видел
        │
        ├─ запись:
        │     ├─ файл вне .app или бандл доступен → write_file_atomic()
        │     └─ бандл защищён (App Management):
        │           stage_bundle() → copytree в /private/tmp
        │           write_file_atomic() в копии
        │           resign_macos_bundle() копии
        │           swap_bundle(): original → .agbak-дата, staged → original
        │
        ├─ backup_path_for() → бэкап ВНЕ бандла
        └─ результат: {ok, edits, states, method, backup, app_backup}
```

### Почему staging, а не запись напрямую

macOS App Management запрещает писать внутрь чужого `.app`-бандла: даже под root
запись даёт `EPERM (Errno 1)`. Переименовать сам бандл при этом можно. Поэтому:

1. бандл копируется в `/private/tmp` (тот же том → `rename` работает);
2. правка вносится в копию;
3. копия переподписывается;
4. оригинал переименовывается в `<name>.agbak-<YYYYmmdd-HHMMSS>`;
5. копия переименовывается на место оригинала.

Оригинал не удаляется — это и есть резервная копия уровня приложения.
Владелец/права копии восстанавливаются явно, иначе бандл станет root-owned.

---

## 4. Поток данных патча IDE

```
do_patch(main_js_path)
        │
        ├─ version check (Info.plist / реестр / package.json)
        ├─ read main.js (bytes)
        ├─ has_foreign_patch()? → предупреждение + подтверждение
        │
        ├─ gates.plan_ide_js(data)
        │     ├─ маркеры стоят? → edits=[], state=patched  (идемпотентность)
        │     ├─ тир-гейт найден? → гейт + онбординг; legacy НЕ трогаем
        │     ├─ legacy-гейт найден? → только он (для 2.5.x)
        │     └─ ничего? → "pattern not found"
        │
        ├─ замена подстрок (длины меняются — применяем последовательно)
        ├─ node --check на временном файле
        │     ошибка → файл НЕ трогаем, выходим
        ├─ _write_backup() → внешнее хранилище
        ├─ _commit_to_bundle()
        │     ├─ бандл доступен → write_file_atomic + resign_macos_bundle
        │     └─ защищён → stage → write → resign → swap
        ├─ очистка CachedData / Code Cache (иначе IDE отдаст старый байткод)
        ├─ пост-проверка: ide_js_state(файл) == "patched"
        └─ панель результата
```

### Почему порядок «тир-гейт, потом legacy»

На IDE 2.6.x в файле присутствуют **оба** паттерна:

* тир-гейт `const s=!!r||!i` (offset `0xbf1ce4` в сборке 2.6.0);
* legacy `resetIsTierGCPTos(),this.<x>.isGoogleInternal` (offset `0xbf13cb`).

Если наложить legacy-патч на тир-гейт, IDE 2.6.x зависает при старте: приложение
уходит в `-[NSAlert runModal]` и не показывает окно. Поэтому legacy применяется
**только** если тир-гейт не найден.

---

## 5. Переподпись (macOS)

`utils/file.py: resign_macos_bundle(main_js_path)`:

1. определяет корень `.app` подъёмом от файла;
2. читает текущие entitlements (`codesign -d --entitlements - --xml`);
3. добавляет `com.apple.security.cs.disable-library-validation` (ad-hoc-подпись без
   Team ID — иначе Electron Framework отвергается проверкой библиотек) и
   `com.apple.security.cs.allow-jit` (иначе V8 не может JIT и Electron падает);
4. собирает все Mach-O и вложенные бандлы (`_macos_signables`), сортирует **от
   вложенных к внешнему**;
5. подписывает каждый `codesign --force --sign - --options runtime --timestamp=none
   --entitlements <plist>`;
6. подписывает сам `.app`;
7. проверяет `codesign --verify --deep --strict`;
8. снимает карантин (`xattr -dr com.apple.quarantine`).

Ключевое: **не `--deep`** (ломает вложенные объекты) и **не без entitlements**
(теряется `allow-jit`).

---

## 6. Хранение бэкапов

| Что | Где |
|---|---|
| Бэкап `main.js` | `~/Library/Application Support/ag_patcher/backups/<hash>-main.js.bak` (macOS), `%APPDATA%\ag_patcher\backups` (Windows), `~/.local/share/ag_patcher/backups` (Linux) |
| Бэкап бинарника вне бандла | Рядом с файлом: `<file>.agybak` |
| Бэкап бинарника внутри бандла | Во внешнем хранилище (иначе ломает codesign-печать) |
| Бэкап всего приложения | `<App>.app.agbak-<YYYYmmdd-HHMMSS>` рядом с оригиналом (при staging-подмене) |

Правило: **ничего нового нельзя класть внутрь `.app` после подписи** — печать
разрывается, и macOS считает приложение повреждённым («улетает в корзину»).

---

## 7. Идемпотентность

Требование: повторный запуск патчера после обновления Antigravity не должен
накладывать старый патч поверх нового.

| Слой | Как обеспечено |
|---|---|
| Байтовые гейты | Патчатся только сигнатуры «до»; сигнатура «после» → статус `patched`, правок нет |
| Прокси | Ищется только `AG_LS_PROXY`; если его нет — правок нет |
| IDE тир-гейт | Перед поиском проверяются маркеры `/*ag_gate*/`, `/*ag_tier*/` |
| IDE legacy | Не применяется, если стоят маркеры тир-патча |
| Бэкапы | Старый бэкап от другой сборки не перетирается, а получает метку времени |

Проверено: второй прогон на всех четырёх живых целях даёт **0 правок**.

---

## 8. Отказ от угадывания

Если сигнатура найдена в нескольких местах — патчер **отказывается** работать
(`LookupError: «сигнатура гейта неоднозначна — отказ от угадывания»`), а не берёт
первое совпадение. Это защита от патча не того участка кода в незнакомой сборке.

Исключение — экраны CLI agy (`allow_multiple=True`): Go-компилятор может продублировать
функцию, и все экземпляры нужно закрыть.

---

## 9. Расширение движка

### Добавить новый байтовый гейт

```python
# patcher/gates.py
NEW_GATE = ByteGate(
    "описание",
    unpatched_regex,   # что ищем ДО патча
    patched_regex,     # что ищем ПОСЛЕ патча (для статуса)
    fix_bytes,         # те же байты, что заменяем
    offset=0,          # смещение внутри найденного совпадения
    allow_multiple=False,
)
```

Добавить в `VALIDATOR_GATES` или `CLI_GATES` — `plan_go_patches()` подхватит сам.

### Добавить новый JS-патч IDE

```python
# patcher/gates.py
NEW_RE  = re.compile(rb"...")
NEW_FIX = b"..."
```
Добавить в `plan_ide_js()` — обязательно в ветку «если тир-гейт не найден», иначе
legacy-патч может наложиться поверх.

### Добавить новую цель

1. `discovery.py` цели — функция поиска пути;
2. `patcher.py` цели — `do_patch_*` / `do_restore_*` через `utils/binary.py`;
3. `cli.py` — секция в `print_target_info()`, пункт в меню PATCH, пункт в RESTORE,
   пункт в подменю «Select custom path».
