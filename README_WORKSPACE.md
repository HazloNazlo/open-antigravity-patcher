# Open AG Patcher — рабочая копия с доработками 1.4.0

Это копия исходников **AvenCores/open-antigravity-patcher** (GPL-3.0), в которой движок
патчей переписан под актуальные версии Antigravity и под macOS ARM.
TUI, меню и общий вид — авторские (AvenCores). Изменена логика патчей.

## Документация

| Файл | О чём |
|---|---|
| **[docs/STATUS.md](docs/STATUS.md)** | Что сделано, что не сделано, что проверено на живых файлах, грабли |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Устройство движка, потоки данных, staging/App Management, переподпись |
| **[docs/PATCHES.md](docs/PATCHES.md)** | Сигнатуры гейтов, живые смещения, сканеры для проверки |
| **[docs/HANDOFF.md](docs/HANDOFF.md)** | Что делать следующему агенту, команды проверки, ограничения среды |

## Быстрый старт

```bash
cd source
python3 -m venv /tmp/agvenv && /tmp/agvenv/bin/pip install -q packaging
/tmp/agvenv/bin/python main.py          # TUI
```

Сборка macOS:

```bash
cd source
/tmp/agvenv/bin/pip install -q pyinstaller
/tmp/agvenv/bin/pyinstaller --onefile --name="Open_AG_Patcher_macOS" --target-arch arm64 \
  --hidden-import=packaging --hidden-import=packaging.version \
  --hidden-import=packaging.specifiers --hidden-import=packaging.requirements \
  --clean main.py
codesign --force --sign - --timestamp=none dist/Open_AG_Patcher_macOS
```

Windows/Linux собираются через `.github/workflows/build-python.yml` (push в GitHub).

## Что делает патчер

Локальные правки файлов, без перехвата сети и без сторонних сервисов:

* **Antigravity IDE** — гейт допустимости тира (`confirmUserForService`) и онбординг
  `free-tier` → `standard-tier`;
* **Antigravity 2.0** (`language_server`) — гейт валидатора `hasValidAuth` + откат
  подмены прокси `AG_LS_PROXY`;
* **Antigravity CLI** (`agy`) — экраны допустимости + валидатор + откат прокси;
* **Бэкенд IDE** (`~/.gemini/bin/agy`, `agy --hub`) — то же, отдельный экземпляр;
* **VS Code extension** — запрет повторной перекачки бинарня.

Патчи идемпотентны; при неоднозначной сигнатуре патчер отказывается работать,
а не угадывает.

## Лицензия

GPL-3.0 — исходный проект AvenCores (см. `LICENSE`, `README.md`).
