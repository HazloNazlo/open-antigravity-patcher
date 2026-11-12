# Сигнатуры гейтов и как их проверять

Все сигнатуры живут в `source/patcher/gates.py`. Этот документ — справочник:
что ищется, где, и как проверить самому.

---

## 1. Гейт валидатора `hasValidAuth`

**Что делает.** `authclient.(*PersonalAuthValidator).Validate` кладёт в `AuthResult`
байт `hasValidAuth` по смещению +8. Пока байт нулевой, клиент уводит выполнение
в ветку «аккаунт не допущен» и не пускает в чат.

### arm64 (Apple Silicon)

```
ldrb  w3, [x0, #8]          ; читает hasValidAuth
tbz   w3, #0, <skip>        ; флаг нулевой — прыгаем мимо успешной ветки
...                        ; 1-2 инструкции контекста
stp   x3, x4, [x0, #0x60]   ; успешная ветка: запись токена
```

Правка: `mov w3,#1` + `strb w3,[x0,#8]` — флаг принудительно True, условный переход
убран, выполнение всегда идёт по успешной ветке.

```python
_ARM64_LDRB_W3_X0_8 = b"\x03\x20\x40\x39"          # ldrb w3, [x0, #8]
_ARM64_MOV_W3_1     = b"\x23\x00\x80\x52"          # mov  w3, #1
_ARM64_STRB_W3_X0_8 = b"\x03\x20\x00\x39"          # strb w3, [x0, #8]
_ARM64_TBZ_W3_ANY_BIT = rb"[\x03\x23\x43\x63\x83\xa3\xc3\xe3]..\x36"
_ARM64_TOKEN_STORE   = rb"(?:....){1,2}\x03\x10\x06\xa9"
```

Длина фикса (8) == длине оригинала (4+4) → in-place.

### x64

```
cmp   byte [rax+8], 0
je    <skip>
mov   rdx, [rsp+disp8]
mov   [rax+0x60], rdx
```

Правка: `mov byte [rax+8],1` + `nop` + `nop`. Длина 6 == 6.

```python
_X64_CMP_BYTE_RAX8 = b"\x80\x78\x08\x00"
_X64_JE = rb"\x74."
_X64_TAIL = rb"\x48\x8b.\x24.\x48\x89.\x60"
_X64_MOV_BYTE_RAX8_1 = b"\xc6\x40\x08\x01"
_X64_NOP2 = b"\x90\x90"
```

### Живые смещения (проверено 2026-11-12)

| Файл | Смещение | Состояние |
|---|---|---|
| `Antigravity.app/Contents/Resources/bin/language_server` (2.31.2) | `0x25a81a0` | patched |
| `~/.gemini/bin/agy` (1.5.6, бэкенд IDE) | `0x2233200` | patched |
| `~/.local/bin/agy` (CLI) | `0x1ea98fc` | patched |

---

## 2. Экраны допустимости CLI agy

Экран «Eligibility check failed:». Сигнатуры различаются по архитектуре и по версии
сборки Go.

| Гейт | Архитектура | Описание |
|---|---|---|
| `CLI_GATE_X64` | x64 | `test rax,rax ; je ; cmp byte[rax+8],0 ; jne` |
| `CLI_GATE_ARM64` | arm64 | `cbnz x1,? ; cbz x0 ; ldrb w1,[x0,#8] ; tbnz w1` |
| `CLI_GATE_ARM64_W4` | arm64 1.2.7+ | `cbz x0 ; ldrb w4,[x0,#8] ; tbnz w4` |
| `CLI_GATE_ARM64_W2` | arm64 1.2.7+ | инлайн-проверка в функции экрана, `ldrb w2` |

Все четыре имеют `allow_multiple=True` — Go может продублировать функцию, и все
экземпляры нужно закрыть.

### Живые смещения (проверено 2026-11-12)

| Файл | Гейт | Смещение | Было | Стало |
|---|---|---|---|---|
| `~/.gemini/bin/agy` | `arm64, w2` | `0x2af0868` | unpatched | **patched** |
| `~/.local/bin/agy` | `arm64` | `0x1fc5b30` | patched (AvenCores) | patched |
| `~/.local/bin/agy` | `arm64, w4` | `0x1fa7f34` | unpatched | **patched** |

После правок `plan_go_patches()` на обоих бинарниках даёт 0 правок — состояние
стабильно patched.

---

## 3. Подмена прокси

Сторонние «разблокировщики» вшивают подмену `https_proxy` (11 байт) на `AG_LS_PROXY`
(11 байт), чтобы гнать трафик через свой локальный прокси. Если их сервис выключен,
приложение шлёт запросы в никуда и выдаёт
«There was a network issue connecting to the server, please try again».

```python
PROXY_ORIGINAL = b"https_proxy"
PROXY_INJECTED = b"AG_LS_PROXY"
```

Длины совпадают → in-place. Откат ищет `AG_LS_PROXY` и заменяет на `https_proxy`.

Проверено: на всех четырёх текущих бинарниках следов подмены нет (уже откачено).

> Если после патча приложение всё ещё показывает «There was a network issue…» —
> значит в бинарнике осталась подмена. Прогнать `plan_go_patches()` со сканера выше:
> в выводе должно быть `proxy hits: [<смещения>]` и правка `revert: AG_LS_PROXY`.
> Если подмены нет — проблема в сокетах VPN, а не в бинарнике (см. `docs/HANDOFF.md`).

---

## 4. JS-гейт Antigravity IDE

### IDE 2.6.x — гейт тира (актуальный)

```js
const r = n?.allowedTiers?.find(l => l.id === "free-tier");
let i = ""; ...
const s = !!r || !i;                              // ← гейт
if (s) {
  await this._cloudCodeMainService.onboardUser("free-tier", t);   // ← онбординг
  ...
}
```

Правки:

```js
const s = !0/*ag_gate*/,                          // всегда «допуск есть»
await this._cloudCodeMainService.onboardUser("standard-tier", t)/*ag_tier*/,
```

* `s = true` — клиент перестаёт проверять допуск к `free-tier`.
* `free-tier` → `standard-tier` — сервер отклоняет онбординг в `free-tier` для
  аккаунтов вне поддерживаемых регионов («not currently available in your location»),
  а `standard-tier` принимает.

```python
IDE_TIER_GATE_RE   = re.compile(rb"(const [A-Za-z_$0-9]+=)!![A-Za-z_$0-9]+\|\|![A-Za-z_$0-9]+([,;])")
IDE_TIER_GATE_FIX  = rb"\1!0/*ag_gate*/\2"
IDE_TIER_ONBOARD_RE  = re.compile(rb'onboardUser\("free-tier",t\)')
IDE_TIER_ONBOARD_FIX = b'onboardUser("standard-tier",t)/*ag_tier*/'
```

### IDE 2.5.x — legacy (запасной вариант)

```js
resetIsTierGCPTos(), this.<x>.isGoogleInternal
```

Правка: `resetIsTierGCPTos(),true`.

```python
IDE_LEGACY_RE  = re.compile(rb"(resetIsTierGCPTos\(\),)this\.[A-Za-z_$0-9]+\.isGoogleInternal")
IDE_LEGACY_FIX = b"resetIsTierGCPTos(),true"
```

**Важно:** legacy применяется, только если тир-гейт не найден. Наложение legacy
на 2.6.x вешает IDE на старте (`NSAlert runModal`).

### Живые смещения (сборка 2.6.0, чистый файл `main.js.ag_backup`)

| Паттерн | Смещение |
|---|---|
| тир-гейт | `0xbf1ce4` |
| онбординг `free-tier` | `0xbf1d58` |
| legacy | `0xbf13cb` |

Все три присутствуют одновременно в одной сборке 2.6.0.

### Маркеры

```python
IDE_PATCH_MARKERS = (b"/*ag_gate*/", b"/*ag_tier*/", b"resetIsTierGCPTos(),true")
```

### Признаки чужой правки

```python
IDE_CUSTOM_MARKERS = (
    b"AG_PROXY_HOOK",
    b'userTier: { id: "pro"',
    b"__res = { settings:",
    b"catch(_) { try { await this._cloudCodeMainService.loadCodeAssist",
)
```

Если найден — предупреждение: патч поверх чужой правки может оставить приложение
нестабильным.

---

## 5. Как проверить сигнатуры самому

Скрипт-сканер по любому бинарнику:

```bash
cd "/Users/sly/Downloads/AvenCores-open-antigravity-patcher-fe12d5b/source"
/tmp/agvenv/bin/python - <<'PY'
import sys, os
sys.path.insert(0, ".")
from patcher.gates import (plan_go_patches, plan_ide_js, ide_js_state,
                           CLI_GATES, find_proxy_reverts)

path = os.path.expanduser("~/.local/bin/agy")
data = open(path, "rb").read()

edits, states = plan_go_patches(data, cli_screens=True, revert_proxy=True)
for label, state in states:
    print(f"{state:10} {label}")
for e in edits:
    print(f"  0x{e.offset:x} {e.label}: {e.before.hex()} -> {e.after.hex()}")
print("proxy hits:", [hex(o) for o, _ in find_proxy_reverts(data)])
PY
```

Скан только состояния (без плана):

```bash
/tmp/agvenv/bin/python - <<'PY'
import sys, os
sys.path.insert(0, ".")
from patcher.gates import CLI_GATES

data = open(os.path.expanduser("~/.gemini/bin/agy"), "rb").read()
for g in CLI_GATES:
    for name, pat in (("unpatched", g.unpatched_re), ("patched", g.patched_re)):
        hits = [hex(m.start() + g.offset) for m in pat.finditer(data)]
        if hits:
            print(f"{g.desc} [{name}]: {hits}")
PY
```

Проверка JS-патча на чистом файле:

```bash
/tmp/agvenv/bin/python - <<'PY'
import sys
sys.path.insert(0, ".")
from patcher.gates import plan_ide_js

p = "/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js.ag_backup"
d = open(p, "rb").read()
edits, states = plan_ide_js(d)
print(states)
for before, after, label in edits:
    print(label, "->", after[:60])
new = d
for before, after, _ in edits:
    new = new.replace(before, after, 1)
open("/tmp/ide_check.js", "wb").write(new)
PY
node --check /tmp/ide_check.js && echo "JS OK"
```

---

## 6. Диагностика «патч не применился»

| Симптом | Причина | Что делать |
|---|---|---|
| `сигнатура гейта не найдена` | Новая версия сборки, изменился код | Обновить сигнатуру в `gates.py`; снять `main.js`/бинарник и сравнить |
| `сигнатура гейта неоднозначна` | Паттерн совпал в нескольких местах | Уточнить сигнатуру (добавить контекст) — не гадать |
| `No patches applied` | Гейт уже закрыт или версия не поддержана | Проверить `status`; если `already patched` — всё ок |
| `JS syntax check failed` | Правка испортила JS | Файл НЕ тронут; исправить regex в `gates.py` |
| `Write error: [Errno 1] Operation not permitted` | App Management | Патчер сам уходит в staging; если нет — закрыть приложение |
| Приложение «улетает в корзину» | Порвана codesign-печать | Не класть файлы внутрь `.app`; переподписать |
| `codesign verify failed` | Плохие entitlements | Проверить `_macos_entitlements()` |
