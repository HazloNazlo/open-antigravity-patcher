# Статус проекта — Open AG Patcher (Antigravity region bypass)

**Версия:** 1.4.0
**Последнее обновление:** 2026-11-12
**Состояние:** рабочий macOS-бинарник собран и проверен; Windows/Linux собираются через CI.

---

## 1. Что это за проект

Копия исходников **AvenCores/open-antigravity-patcher** (GPL-3.0), в которой движок
патчей переписан так, чтобы работать с актуальными версиями Antigravity (2.31.2 / IDE 2.6.0 /
CLI 1.5.x) и под macOS ARM. TUI, меню и общий вид оставлены авторскими (AvenCores) —
изменена только логика патчей, переподпись и набор целей.

**Легитимные цели патча (только локальные правки файлов):**

| Продукт | Что патчится |
|---|---|
| Antigravity IDE (`main.js`) | Гейт допустимости тира `confirmUserForService` + онбординг `free-tier` → `standard-tier` |
| Antigravity 2.0 (`language_server`) | Гейт валидатора `hasValidAuth` + откат подмены прокси |
| Antigravity CLI (`~/.local/bin/agy`) | Экраны допустимости CLI + `hasValidAuth` + откат прокси |
| Бэкенд IDE (`~/.gemini/bin/agy`, `agy --hub`) | То же, что CLI — это отдельный экземпляр agy |
| VS Code extension (`extension.js`) | Запрет повторной перекачки бинарня (AvenCores, без изменений) |

Сеть не перехватывается, ключи и сторонние сервисы не используются. Требуется рабочий
аккаунт Google и серверная поддержка аккаунта (она принимает `standard-tier`).

---

## 2. Что сделано (готово и проверено)

### 2.1 Движок патчей — `source/patcher/gates.py` (НОВЫЙ ФАЙЛ)

Единый модуль со всеми сигнатурами. До правки сигнатуры дублировались в трёх местах
(`manager/patcher.py`, `agy/patcher.py`, `ide/patcher.py`) и разошлись.

* `ByteGate` — байтовый гейт: сигнатура «до» → сигнатура «после». Фикс всегда равен
  длине оригинала → запись in-place, файл не растёт и не сдвигает соседние участки.
  При нескольких совпадениях — **отказ**, а не выбор «первого попавшегося».
* `VALIDATOR_GATES` — гейт `hasValidAuth` (arm64 + x64).
* `CLI_GATES` — экраны допустимости CLI agy (x64, arm64, arm64/w2, arm64/w4).
* `plan_go_patches(data, cli_screens, revert_proxy)` — план правок для Go-бинарника.
* `plan_ide_js(data)` — план правок `main.js`.
* `apply_edits(data, edits)` — применение с конца к началу + проверка, что байты на
  месте те же, что видел планировщик (защита от правки извне).
* `find_proxy_reverts()` — откат подмены `AG_LS_PROXY` → `https_proxy`.

### 2.2 Общий модуль бинарного патча — `source/patcher/utils/binary.py` (НОВЫЙ ФАЙЛ)

`patch_binary()` / `restore_binary()` / `find_backup()` / `backup_path_for()` —
используются и менеджером, и agy.

* Бэкапы файлов внутри `.app` хранятся **вне бандла** (любой новый файл внутри `.app`
  после подписи ломает codesign-печать → приложение «улетает в корзину»).
* Старый бэкап от другой сборки **не перетирается**: появляется новый с меткой времени.
* `macOS App Management`: если бандл недоступен для записи — копия бандла в
  `/private/tmp`, правка в копии, переподпись, подмена целиком через `rename`
  (оригинал остаётся как `<name>.agbak-<YYYYmmdd-HHMMSS>`).
* Владелец/права копии восстанавливаются явно (`_preserve_ownership`) — иначе
  скопированный под sudo бандл становится root-owned.

### 2.3 Утилиты файлов — `source/patcher/utils/file.py`

Добавлено:

* `bundle_writable()` — проба записи внутрь бандла (детект App Management).
* `stage_bundle()` / `swap_bundle()` / `cleanup_stage()` — staging + подмена бандла.
* `check_js_syntax()` — проверка JS через `node --check` (если node нет — пропуск).
* `write_file_atomic()` — атомарная запись с сохранением прав/владельца.
* `backup_store_dir()` — внешнее хранилище бэкапов (macOS: `~/Library/Application Support/ag_patcher/backups`).
* `app_bundle_of()` — поиск корня `.app` вверх по пути.
* `patch_file_in_bundle()` — единая точка записи с учётом App Management.

Ранее переписано: `resign_macos_bundle()` — переподпись **изнутри наружу**, с сохранением
entitlements + `allow-jit` + `disable-library-validation`, без `--deep`.

### 2.4 IDE-патчер — `source/patcher/ide/patcher.py` (ПЕРЕПИСАН)

* Поддержка **2.6.x** (гейт тира + `standard-tier`) и **2.5.x** (legacy `isGoogleInternal`).
* **Legacy-патч никогда не накладывается поверх тир-гейта** — на IDE 2.6.x это вешает
  приложение на старте в модальном окне `NSAlert`.
* Идемпотентность: при наличии маркеров повторный запуск ничего не меняет.
* `node --check` **до** записи; при ошибке синтаксиса файл не трогается.
* Пост-проверка: маркеры должны стоять в файле после записи.
* Бэкап `main.js` — во внешнем хранилище, не внутри бандла.
* Предупреждение, если в файле есть следы чужого «разблокировщика» (`IDE_CUSTOM_MARKERS`).

### 2.5 Менеджер и agy — `manager/patcher.py`, `agy/patcher.py` (ПЕРЕПИСАНЫ)

* Оба используют общий движок `gates.py` + `utils/binary.py`.
* `agy` патчит **полный набор** гейтов: экраны CLI + `hasValidAuth` + откат прокси.
  Раньше патчился только экран — этого было недостаточно.
* Статус учитывает **все** гейты: частично пропатченный бинарник показывается как
  «not patched», а не «already patched».
* Убрано опасное копирование пропатченного бинарника в `~/.local/bin` — оно затирало
  CLI-версию бинарником бэкенда IDE.
* `do_patch_agy(path, is_hub=True)` — режим бэкенда IDE.

### 2.6 Новая цель: бэкенд Antigravity IDE

* `patcher/agy/discovery.py`: `find_ide_backend_binary()` → `~/.gemini/bin/agy`.
* `patcher/cli.py`: новая секция панели **ANTIGRAVITY IDE BACKEND**, пункты меню
  `[4]` PATCH и `[9]` RESTORE, пункт `[6]` в подменю «Select custom path».
* Нумерация меню расширена с 12 до 14 пунктов.

### 2.7 TUI (`patcher/cli.py`)

Вид AvenCores сохранён полностью (рамка-шапка, секции PATCH/RESTORE/TOOLS,
`[1]…[14]`, footer «Tip: patches are reversible…», prompt «Select option >»).
Добавлена пятая секция статуса.

### 2.8 Версия

`patcher/constants.py` → `VERSION = "1.4.0"`; `version.txt` → 1.4.0.0.

### 2.10 Совместимость с upstream-тестом `test-agy`

Тест `test-agy/test_cli_releases.py` (авторский, проверяет гейты по всем официальным
сборкам `google-antigravity/antigravity-cli`) импортирует `patcher.agy.patcher` и
использует `ALL_GATES` + `resolve_all()`.

После переноса гейтов в `gates.py` старый API сохранён:

* `patcher.agy.patcher.ALL_GATES` → `[(CLI_GATE_GROUP, "eligibility screen off")]`;
* `ByteGate.resolve_all(data)` → `[(kind, offsets, gate)]`;
* `GateGroup` — группа вариантов гейта (архитектура/версия), возвращает все совпавшие.

Дополнительно `get_status()` в `agy/patcher.py` сохранён в upstream-семантике
(только экраны CLI + прокси), а полный статус с валидатором вынесен в
`get_status_detail()`. `is_already_patched()` использует полную проверку.

Проверено round-trip на живых бинарниках: `unpatched → patched → unpatched`,
размер файла не меняется.

**Upstream-тест `test-agy` пройден полностью (2026-11-12):**

```
Ran 3 tests in 271.091s — OK
Release: 1.5.6 (8 archives)
  linux arm64 / linux arm64-musl / linux x64 / linux x64-musl
  mac arm64 / mac x64 / windows arm64 / windows x64
```

Значит сигнатуры CLI-гейтов корректно детектятся и патчатся **на всех официальных
платформах**, включая Windows и Linux (проверяется round-trip на копиях).
Кэш скачанных архивов (`AGY_TEST_CACHE`) после прогона удалён — он занимает 2.6 ГБ.

### 2.11 CI — `.github/workflows/build-python.yml`* Во все сборки добавлены `--hidden-import=packaging.*` (раньше были только в Linux/macOS —
  на Windows был риск `ModuleNotFoundError: packaging`).
* В macOS-джоб добавлены: переподпись ad-hoc после сборки, `codesign --verify --strict`,
  `lipo -archs` и headless smoke-тест запуска.

---

## 3. Проверено на живых файлах

| Цель | До | После | Проверка |
|---|---|---|---|
| `~/.local/bin/agy` (CLI) | гейт `arm64/w4` открыт; **подпись невалидна → процесс убивался SIGKILL** | все гейты закрыты, подпись валидна | `agy --version` → `1.1.14`, exit 0 |
| `~/.gemini/bin/agy` (бэкенд IDE) | гейт `arm64/w2` открыт | все гейты закрыты | `agy --version` → `1.5.6`, exit 0 |
| `Antigravity.app/.../language_server` | `hasValidAuth` закрыт | без изменений (идемпотентность) | `codesign -v` OK, `language_server --help` работает |
| `Antigravity IDE.app/.../main.js` | тир-гейт + `standard-tier` стоят | без изменений | `node --check` OK, маркеры на месте |

* `codesign -v` — валидно для обоих `.app` и для обоих бинарников agy.
* Идемпотентность движка: второй прогон на всех целях даёт **0 правок**.
* JS-патч, применённый к чистому бэкапу `main.js.ag_backup`, проходит `node --check`.
* TUI собранного бинарника запускается, показывает 5 целей со статусом «already patched».
* Round-trip совместимости с `test-agy`: `unpatched → patched → unpatched`, размер
  файла не меняется.

---

## 4. Собранные бинарники

```
source/dist/Open_AG_Patcher_macOS   — universal2 (x86_64 + arm64), ~13.9 МБ, подписан ad-hoc
```

Собран локально из python.org universal2-Python 3.12.10 (распакован в `/tmp/pyfw`,
система не изменялась). Подпись после сборки пересоздана `codesign --force --sign -`
и проверена `codesign --verify --strict`.

Проверено: `lipo -archs` → `x86_64 arm64`, бинарник запускается, TUI отрисовывается.

---

## 5. Что НЕ сделано / что осталось

### 5.1 Windows и Linux — нужен push в GitHub

Локально кросс-сборка невозможна (PyInstaller не кросс-компилится), но workflow
подготовлен и лежит в `.github/workflows/build-python.yml`.

**Блокер:** в рабочей папке **нет `.git`** — это распакованная копия, а не git-репозиторий.
Чтобы собрать Windows/Linux:

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b"
git init && git remote add origin <URL-репозитория-AvenCores>
git add -A && git commit -m "v1.4.0: единый движок гейтов, IDE 2.6.x, бэкенд agy, App Management staging"
git push -u origin main
```

Далее → Actions → «Build Python» → скачать артефакты
`python-windows-x64`, `python-windows-arm64`, `python-linux-x64`, `python-linux-arm64`,
`python-macos-universal2`.

**Альтернатива без GitHub:** запустить workflow вручную (`workflow_dispatch`) или
перенести workflow в свой репозиторий — он самодостаточен.

### 5.2 Не проверено на живых Windows/Linux

Сигнатуры CLI-гейтов проверены на всех 8 официальных сборках релиза 1.5.6
(linux/mac/windows × x64/arm64) — см. upstream-тест `test-agy` в разделе 2.10.
Но не проверено:
* запись в `%ProgramFiles%` и `/usr/share` (нужен root/admin — в CI это обеспечивается
  `--uac-admin` и запуском от root);
* поведение `is_locked()` на Windows с запущенным приложением;
* отсутствие codesign-аналогов — на этих ОС подписи нет, нужен только chmod;
* переподпись — на этих ОС её нет вовсе.

### 5.3 IDE 2.5.x — legacy-ветка не проверена на живом файле

Код legacy-патча (`resetIsTierGCPTos(),true`) написан и сигнатура найдена в сборке 2.6.0,
но на реальном IDE 2.5.x не применялся (такой версии на машине нет). Риск: тот же
legacy-патч на 2.6.x вешает IDE — поэтому на 2.6.x он и не применяется.

### 5.4 `~/.gemini/bin/antigravity` (Windows-имя бинарника бэкенда)

`find_ide_backend_binary()` ищет `agy` и `antigravity` в `~/.gemini/bin` — на macOS
найден `agy`. На Windows имя может быть `antigravity.exe` — код это учитывает,
но на живой Windows не проверялось.

### 5.5 Прочее

* README репозитория обновлён: добавлен раздел с ключевыми улучшениями версии 1.4.0.
* `ag-patcher-mac/` (headless-патчер) синхронизирован: добавлены 4 сигнатуры экранов допустимости CLI (`CLI_GATES`), обновлён вывод статуса для `cli`/`hub` и версия поднята до 1.4.0.
* Неиспользуемый файл `source/patcher/utils/atomic.py` удалён через `mavis-trash`.

---

## 6. Известные грабли (не повторять)

| Грабли | Симптом | Правильное решение |
|---|---|---|
| Legacy-патч поверх тир-гейта | IDE 2.6.x зависает на старте (`NSAlert runModal`) | Сначала искать тир-гейт; legacy — только если его нет |
| Байтовая замена другой длины | Съедает соседний код (`o is not defined`) | Вырезать `len(before)`, а не `len(after)`; применять с конца к началу |
| Файл-журнал внутри `.app` | codesign-печать рвётся, приложение «улетает в корзину» | Журналы и бэкапы — только вне бандлов |
| `codesign --deep` без entitlements | Теряется `allow-jit` → V8 без JIT → падение | Подписывать изнутри наружу, сохраняя entitlements |
| Запись внутрь чужого `.app` | EPERM даже под root (App Management) | Staging-копия + подмена через `rename` |
| `;` в `const s=…,o=…` | Рвёт объявление | Не вставлять `;` внутрь объявления |
| `codesign --deep` при проверке | Ложные «ошибки» на вложенных объектах | Проверять `--deep --strict` у внешнего бандла |
| `mv` через `cp` при подписи | SIGKILL «Code Signature Invalid» | `codesign --force --sign -` после любой правки файла |

---

## 7. Быстрый старт для следующего агента

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b/source"

# Запуск TUI из исходников (нужен venv с packaging)
python3 -m venv /tmp/agvenv && /tmp/agvenv/bin/pip install -q packaging
/tmp/agvenv/bin/python main.py

# Сборка macOS
/tmp/agvenv/bin/pip install -q pyinstaller
/tmp/agvenv/bin/pyinstaller --onefile --name="Open_AG_Patcher_macOS" --target-arch arm64 \
  --hidden-import=packaging --hidden-import=packaging.version \
  --hidden-import=packaging.specifiers --hidden-import=packaging.requirements \
  --clean main.py
codesign --force --sign - --timestamp=none dist/Open_AG_Patcher_macOS
```

Проверить состояние целей без GUI:

```bash
/tmp/agvenv/bin/python - <<'PY'
import sys; sys.path.insert(0, ".")
from patcher.ide.patcher import is_already_patched
from patcher.agy.patcher import get_status
from patcher.manager.patcher import get_status as mgr
p = "/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js"
print("IDE:", is_already_patched(open(p,'rb').read()))
print("CLI:", get_status("/Users/sly/.local/bin/agy"))
print("hub:", get_status("/Users/sly/.gemini/bin/agy"))
print("mgr:", mgr("/Applications/Antigravity.app/Contents/Resources/bin/language_server"))
PY
```

---

## 8. Карта файлов

```
source/
  main.py                       Точка входа, elevation, обработка ошибок
  build.txt                     Команды PyInstaller под 3 ОС
  version.txt                   VSVersionInfo для Windows (1.4.0)
  requirements.txt              pyinstaller, packaging
  patcher/
    cli.py                      TUI: панель статуса, меню 1..14, обработчики
    constants.py                VERSION, MIN_*_VERSION, цвета
    gates.py                 ★  ЕДИНЫЙ ДВИЖОК: сигнатуры, план правок, применение
    utils/
      file.py                ★  Файл-операции, staging/swap, переподпись, бэкапы
      binary.py              ★  Общий патч бинарников (manager + agy)
      console.py                Вывод: рамки, секции, step/ok/warn
      admin.py                 Завершение процессов, elevation
      atomic.py                Старый хелпер AvenCores (не используется новым движком)
      update.py                Проверка обновлений GitHub, handle_patch_failure
      captcha.py               Подтверждение операций
    ide/
      discovery.py               Поиск IDE, версия, main.js
      patcher.py             ★  IDE-патчер: 2.6.x / legacy, node --check, staging
    manager/
      discovery.py               Поиск language_server, чтение app.asar
      patcher.py             ★  Патч менеджера через общий движок
    agy/
      discovery.py            ★  + find_ide_backend_binary() (~/.gemini/bin/agy)
      patcher.py             ★  Патч agy (CLI и бэкенд IDE)
    vscode/
      discovery.py               Расширение google.google-antigravity
      patcher.py                 extension.js (AvenCores, без изменений)
ag-patcher-mac/
  ag_patcher.py                   Headless-патчер (status/patch/restore/doctor)
  README.md
AGENTS.md                         Входные данные для ИИ-агента (краткая сводка)
README_WORKSPACE.md               Указатель на документацию (не переписывает авторский README)
docs/
  STATUS.md                    ★  Этот файл: что сделано / что не сделано
  ARCHITECTURE.md                 Устройство движка, потоки данных, staging, переподпись
  PATCHES.md                     Сигнатуры, живые смещения, сканеры для проверки
  HANDOFF.md                      Что делать следующему агенту, команды, ограничения
.github/workflows/build-python.yml  CI: Win/Linux/macOS
```
