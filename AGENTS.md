# AGENTS.md — входные данные для ИИ-агента

## Что это

Рабочая копия **Open AG Patcher** — патчера региональных ограничений Google Antigravity.
Исходный проект: AvenCores/open-antigravity-patcher (GPL-3.0). Здесь переписан движок
патчей: версия 1.4.0.

**Путь проекта:** `/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b`

## Обязательно прочитать

1. `docs/STATUS.md` — что сделано / что нет / что проверено
2. `docs/ARCHITECTURE.md` — как устроен движок
3. `docs/PATCHES.md` — сигнатуры и сканеры
4. `docs/HANDOFF.md` — задачи следующего этапа

## Текущая задача

Доработать патчер до вида AvenCores с нормальным запуском под Windows, Linux и Mac.

**Сделано:** единый движок гейтов, IDE 2.6.x + legacy, полный набор гейтов agy,
бэкенд IDE как отдельная цель, обход macOS App Management (staging + swap),
корректная переподпись, macOS universal2-бинарник собран и проверен.

**Осталось:** Windows/Linux-бинарники (нужен push в GitHub — в папке нет `.git`),
живая проверка на Windows/Linux, проверка legacy-ветки на IDE 2.5.x.

## Жёсткие ограничения

* Правки **только локальные**: без прокси, ключей, перехвата TLS, сторонних сервисов.
* Патчи **идемпотентны**: повторный запуск не накладывает старый патч поверх нового.
* При **неоднозначной сигнатуре — отказ**, а не выбор «первого попавшегося».
* **Ничего нельзя класть внутрь `.app` после подписи** — рвётся codesign-печать.
* Переподпись: **изнутри наружу**, с entitlements, **без `--deep`**.
* IDE 2.6.x: **legacy-патч нельзя накладывать поверх тир-гейта** (вешает IDE).
* Удаление файлов — только `/Users/sly/.minimax/bin/mavis-trash -- <paths>`.
* Не трогать «Рабочий ПК» и внешний диск «Data» без прямой просьбы.

## Среда

* macOS 27.2, ARM (Apple M4), внутренний диск заполнен (33 ГиБ свободно).
* VPN (AmneziaWG, выход US) работает, GitHub доступен.
* Python: системный 3.9.6 без `packaging` → использовать `/tmp/agvenv`
  (`python3 -m venv /tmp/agvenv && /tmp/agvenv/bin/pip install -q packaging pyinstaller`).
* `node` v24 есть — используется для `node --check` валидации JS-патча.

## Команды

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b/source"

# Запуск TUI из исходников
/tmp/agvenv/bin/python main.py

# Статус целей
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

# Сборка macOS universal2
/tmp/agvenv/bin/pyinstaller --onefile --name="Open_AG_Patcher_macOS" --target-arch universal2 \
  --hidden-import=packaging --hidden-import=packaging.version \
  --hidden-import=packaging.specifiers --hidden-import=packaging.requirements --clean main.py
codesign --force --sign - --timestamp=none dist/Open_AG_Patcher_macOS
```

## Цели патча на этой машине

| Путь | Продукт | Состояние |
|---|---|---|
| `/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js` | IDE 2.6.0 | patched |
| `/Applications/Antigravity.app/Contents/Resources/bin/language_server` | Manager 2.31.2 | patched |
| `~/.local/bin/agy` | CLI | patched |
| `~/.gemini/bin/agy` | Бэкенд IDE (`agy --hub`) | patched |

Бэкапы: `~/Library/Application Support/ag_patcher/backups/` и `<file>.agybak` рядом
с бинарниками вне бандлов.
