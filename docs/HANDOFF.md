# HANDOFF — что делать следующему агенту

Ты продолжаешь работу над **Open AG Patcher** — патчером региональных ограничений
Google Antigravity (IDE 2.6.x, Antigravity 2.0 manager, CLI agy, бэкенд IDE agy,
VS Code extension).

**Папка проекта:**
`/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b`

**Прочитай сначала:** `docs/STATUS.md` (что сделано / что нет),
`docs/ARCHITECTURE.md` (устройство), `docs/PATCHES.md` (сигнатуры).

---

## 1. Контекст в одном абзаце

Это копия исходников **AvenCores/open-antigravity-patcher** (GPL-3.0). TUI и общий вид
оставлены авторскими — задача была в том, чтобы движок патчей заработал с актуальными
версиями Antigravity и под macOS ARM. Сделано: единый движок гейтов, поддержка
IDE 2.6.x, патч бэкенда IDE, обход App Management через staging, корректная
переподпись. macOS-бинарник собран и проверен. Windows/Linux ждут push в GitHub.

---

## 2. Главное, что нужно понять про этот код

1. **Правки локальные, сеть не трогается.** Никаких прокси, ключей, TLS-перехвата.
   Пользователю нужен рабочий аккаунт Google, чей сервер принимает `standard-tier`.

2. **Патчи идемпотентны.** Повторный запуск не должен накладывать старый патч поверх
   нового. Если добавляешь гейт — обеспечь это.

3. **При неоднозначной сигнатуре — отказ, а не угадывание.** Это осознанное решение:
   патч не того участка кода в незнакомой сборке хуже, чем отказ.

4. **Ничего нельзя класть внутрь `.app` после подписи.** Новый файл внутри бандла рвёт
   codesign-печать → macOS считает приложение повреждённым («улетает в корзину»).
   Бэкапы и журналы — только во внешнем хранилище.

5. **`codesign --deep` не использовать** при переподписи, и **нельзя без entitlements**
   (теряется `allow-jit` → V8 без JIT → Electron падает).

6. **IDE 2.6.x: legacy-патч нельзя накладывать поверх тир-гейта** — приложение
   зависает на старте в модальном окне `NSAlert`.

---

## 3. Текущее состояние

| Пункт | Статус |
|---|---|
| Единый движок гейтов (`gates.py`) | ✅ готово |
| IDE 2.6.x (тир-гейт + `standard-tier`) | ✅ готово, применено на живой IDE |
| IDE 2.5.x (legacy) | ⚠️ код написан, на живой версии не проверен |
| Менеджер (`language_server`) | ✅ готово, применено |
| CLI agy (полный набор гейтов) | ✅ готово, применено |
| Бэкенд IDE (`~/.gemini/bin/agy`) | ✅ готово, применено |
| Обход App Management (staging + swap) | ✅ готово |
| Переподпись (изнутри наружу, entitlements) | ✅ готово, проверено |
| TUI в стиле AvenCores | ✅ готово, 5 целей, меню 1..14 |
| macOS universal2-бинарник | ✅ собран в `source/dist/` |
| Синхронизация `ag-patcher-mac/` | ✅ готово (добавлены `CLI_GATES`, v1.4.0) |
| README под 1.4.0 | ✅ готово (добавлена плашка с описанием 1.4.0) |
| Windows/Linux-бинарники | ✅ собраны в GitHub Actions (4 артефакта) |
| Живая проверка на Windows/Linux | ❌ не делалась |

Подробности — `docs/STATUS.md`, раздел 5.

---

## 4. Что делать дальше (по приоритету)

### Задача A — Собрать Windows и Linux (нужен доступ к GitHub)

**Блокер:** в папке нет `.git` — это распакованная копия. Сборка кросс-компиляцией
невозможна (PyInstaller не умеет), поэтому нужен push в репозиторий, где уже есть
workflow `.github/workflows/build-python.yml` (подготовлен и обновлён под 1.4.0).

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b"
git init
git remote add origin <URL-репозитория>
git add -A
git commit -m "v1.4.0: единый движок гейтов, IDE 2.6.x, бэкенд agy, App Management staging"
git push -u origin main
```

Дальше: GitHub → Actions → «Build Python» → артефакты
`python-windows-x64`, `python-windows-arm64`, `python-linux-x64`,
`python-linux-arm64`, `python-macos-universal2`.

**Если доступа к репозиторию AvenCores нет:** перенеси workflow в свой репозиторий —
он самодостаточен (нужны только `source/` и `.github/workflows/build-python.yml`).

**Если push невозможен:** можно собрать Windows/Linux через виртуалку/контейнер с
соответствующей ОС и тем же PyInstaller — workflow даёт точные команды.

### Задача B — Проверить Windows/Linux логику

Что стоит проверить на реальной Windows и Linux:

* запись в `%ProgramFiles%\Antigravity IDE` и `/usr/share/antigravity-ide`
  (нужен admin/root — в CI это есть);
* `is_locked()` — на Windows бинарник может быть занят запущенным приложением;
* поведение `backup_store_dir()`: `%APPDATA%\ag_patcher\backups` и
  `~/.local/share/ag_patcher/backups`;
* отсутствие codesign-аналогов — на этих ОС подписи нет, нужен только chmod;
* `node --check` — если node не установлен, проверка корректно пропускается
  (`check_js_syntax` возвращает `ok=True`).

### Задача C — Проверить IDE 2.5.x (legacy-ветка)

На машине нет IDE 2.5.x. Код legacy-патча есть, сигнатура в сборке 2.6.0 тоже
присутствует (`0xbf13cb`), но на 2.5.x не применялся. Если будет доступна сборка
2.5.x — применить и убедиться, что IDE запускается.

### Задача D — README под 1.4.0

`README.md` в корне репозитория — авторский текст AvenCores (61 КБ). Не переписывать
его целиком; можно добавить раздел про изменения 1.4.0 (новая цель «IDE backend»,
обход App Management, полный набор гейтов agy).

### Задача E — Синхронизация `ag-patcher-mac/`

Headless-патчер (`ag-patcher-mac/ag_patcher.py`) работает и не связан с `gates.py`,
но использует те же сигнатуры. При изменении гейтов в `gates.py` нужно поправить и
его (или сделать его тонкой обёрткой над `gates.py`).

---

## 5. Как проверить, что ничего не сломано

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b/source"

# 1. Компиляция всех модулей
for f in $(find patcher -name "*.py"); do /tmp/agvenv/bin/python -m py_compile "$f" || echo "FAIL $f"; done

# 2. Статус целей (ожидается: IDE patched, CLI patched, hub patched, mgr patched)
/tmp/agvenv/bin/python - <<'PY'
import sys; sys.path.insert(0, ".")
from patcher.ide.patcher import is_already_patched
from patcher.agy.patcher import get_status
from patcher.manager.patcher import get_status as mgr
print("IDE :", is_already_patched(open("/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js","rb").read()))
print("CLI :", get_status("/Users/sly/.local/bin/agy"))
print("hub :", get_status("/Users/sly/.gemini/bin/agy"))
print("mgr :", mgr("/Applications/Antigravity.app/Contents/Resources/bin/language_server"))
PY

# 3. Идемпотентность: plan_go_patches на живых файлах должен дать 0 правок
/tmp/agvenv/bin/python - <<'PY'
import sys, os; sys.path.insert(0, ".")
from patcher.gates import plan_go_patches
for p, cli in (("/Users/sly/.local/bin/agy", True),
               ("/Users/sly/.gemini/bin/agy", True),
               ("/Applications/Antigravity.app/Contents/Resources/bin/language_server", False)):
    edits, _ = plan_go_patches(open(p,"rb").read(), cli_screens=cli)
    print(f"{os.path.basename(p):16} edits={len(edits)} (ожидается 0)")
PY

# 4. Подписи
codesign -v "/Applications/Antigravity.app" && echo "manager app OK"
codesign -v "/Applications/Antigravity IDE.app" && echo "IDE app OK"
codesign -v ~/.local/bin/agy && echo "CLI agy OK"
codesign -v ~/.gemini/bin/agy && echo "hub agy OK"

# 5. JS-синтаксис
node --check "/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js" && echo "main.js OK"

# 6. TUI
script -q /dev/null ./dist/Open_AG_Patcher_macOS < /dev/null > /tmp/tui.txt 2>&1
```

---

## 6. Полезные команды

```bash
# Подготовка venv
python3 -m venv /tmp/agvenv
/tmp/agvenv/bin/pip install -q packaging pyinstaller

# Запуск из исходников
cd source && /tmp/agvenv/bin/python main.py

# Сборка macOS universal2 (нужен universal2-Python; CI делает это сам)
cd source && /tmp/agvenv/bin/pyinstaller --onefile --name="Open_AG_Patcher_macOS" \
  --target-arch universal2 --hidden-import=packaging --hidden-import=packaging.version \
  --hidden-import=packaging.specifiers --hidden-import=packaging.requirements --clean main.py
codesign --force --sign - --timestamp=none dist/Open_AG_Patcher_macOS

# Запуск TUI под pty для снятия вида (как в AvenCores)
script -q /dev/null ./dist/Open_AG_Patcher_macOS < /dev/null > /tmp/tui.txt 2>&1
iconv -f utf-8 -t utf-8 -c /tmp/tui.txt | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | sed 's/\r//g'

# Удаление файлов (только так!)
"/Users/sly/.minimax/bin/mavis-trash" -- <paths>
```

---

## 7. Ограничения окружения

* **Диск заполнен** (33 ГиБ свободно). Бэкап бандла ≈ 490 МБ (manager) / 615 МБ (IDE).
  Не создавать тяжёлых копий без нужды.
* **Внешний диск «Data»** отваливается под нагрузкой — не трогать без просьбы.
* **«Рабочий ПК»** (357 ГБ, глубокая вложенность) — не трогать без просьбы.
* **Удаление** — только через `/Users/sly/.minimax/bin/mavis-trash`.
* **Python** — системный 3.9.6 не имеет `packaging`; использовать `/tmp/agvenv`.
  PyInstaller ставить в тот же venv.
* **VPN** (AmneziaWG, выход US) работает; GitHub доступен.

---

## 8. Если что-то пошло не так

| Симптом | Что делать |
|---|---|
| Приложение «улетает в корзину» | Проверить `codesign -v`; не добавлять файлы внутрь `.app`; переподписать |
| IDE зависает при старте (NSAlert) | Наложен legacy-патч поверх тир-гейта — откатить через RESTORE |
| «There was a network issue…» | В бинарнике осталась подмена `AG_LS_PROXY` — прогнать патч ещё раз |
| `Errno 1 Operation not permitted` | App Management: закрыть приложение и повторить (patcher сам уйдёт в staging) |
| `ModuleNotFoundError: packaging` | Использовать venv с `packaging`; в CI добавлены `--hidden-import=packaging.*` |
| Бинарник убивается сразу (SIGKILL) | Порвана подпись → `codesign --force --sign - <file>` |
| `lipo`/`cmp` на файле в `dist/` убиты | Гонка с записью файла — просто повторить команду |
