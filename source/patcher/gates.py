"""Единый движок патчей региональных ограничений Antigravity.

Здесь собраны все сигнатуры и правила правки — менеджер (Antigravity 2.0),
CLI agy, бэкенд Antigravity IDE и сам IDE (main.js) используют один и тот же
код, поэтому две реализации не могут разойтись.

Что здесь описано
------------------
1. Гейт валидатора hasValidAuth (arm64 + x64) — Go-бэкенды.
2. Откат подмены прокси (AG_LS_PROXY -> https_proxy) — Go-бэкенды.
3. Гейты экрана допустимости CLI agy (x64 + arm64, несколько вариантов).
4. JS-патчи Antigravity IDE: тир-гейт 2.6.x и legacy-ветка 2.5.x.

Принципы
--------
* Патчи на уровне байт-сигнатур, а не смещений: версия сборки может
  сместить код, сигнатура — нет.
* Длина фикса всегда равна длине оригинала: байтовый патч выполняется
  in-place, файл не растёт и не сдвигает соседние участки.
* Неоднозначная сигнатура (несколько совпадений) — отказ от правки,
  а не выбор «первого попавшегося».
* Идемпотентность: уже применённый патч распознаётся и второй раз
  не накладывается (в частности, legacy-патч не вешается поверх
  тир-гейта 2.6.x — это ломает IDE на старте).
"""

import re


# --------------------------------------------------------------------------- #
#  Определение целевой архитектуры бинарника                                  #
# --------------------------------------------------------------------------- #

def detect_binary_arch(data: bytes) -> str:
    """Определяет архитектуру бинарника: 'x64' | 'arm64' | 'universal' | 'unknown'.

    Поддерживает:
      * PE (Windows .exe / .dll)
      * ELF (Linux)
      * Mach-O (macOS single-arch & fat/universal)
    """
    if not data or len(data) < 8:
        return "unknown"

    # 1. PE / MZ (Windows)
    if len(data) >= 0x40 and data[:2] == b"MZ":
        pe_offset = int.from_bytes(data[0x3c:0x40], "little")
        if len(data) >= pe_offset + 6 and data[pe_offset:pe_offset + 4] == b"PE\x00\x00":
            machine = int.from_bytes(data[pe_offset + 4:pe_offset + 6], "little")
            if machine == 0x8664:
                return "x64"
            if machine == 0xAA64:
                return "arm64"
            if machine == 0x014C:
                return "x86"

    # 2. ELF (Linux)
    if len(data) >= 0x14 and data[:4] == b"\x7fELF":
        machine = int.from_bytes(data[0x12:0x14], "little")
        if machine == 0x3E:
            return "x64"
        if machine == 0xB7:
            return "arm64"

    # 3. Mach-O (macOS)
    magic = data[:4]
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        return "universal"
    if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        endian = "little" if magic == b"\xcf\xfa\xed\xfe" else "big"
        cputype = int.from_bytes(data[4:8], endian)
        if cputype == 0x01000007:
            return "x64"
        if cputype == 0x0100000C:
            return "arm64"

    return "unknown"


# --------------------------------------------------------------------------- #
#  Подмена прокси                                                            #
# --------------------------------------------------------------------------- #

# Сторонние «разблокировщики» вшивают в бинарник подмену https_proxy (11 байт)
# на AG_LS_PROXY (11 байт), чтобы гнать трафик через свой локальный прокси.
# Если их сервис выключен, приложение шлёт запросы в никуда и выдаёт
# «There was a network issue connecting to the server, please try again».
# Подмена обратима: длины совпадают, сигнатура байт-в-байт.
PROXY_ORIGINAL = b"https_proxy"
PROXY_INJECTED = b"AG_LS_PROXY"


def find_proxy_reverts(data):
    """Смещения подмены https_proxy -> AG_LS_PROXY.

    Возвращает список (offset, original_bytes). Если подмены нет — пустой список.
    """
    out = []
    for match in re.finditer(re.escape(PROXY_INJECTED), data):
        out.append((match.start(), PROXY_ORIGINAL))
    return out


def proxy_revert_count(data):
    return len(find_proxy_reverts(data))


# --------------------------------------------------------------------------- #
#  Гейт валидатора hasValidAuth                                                #
# --------------------------------------------------------------------------- #

# authclient.(*PersonalAuthValidator).Validate кладёт в AuthResult байт
# hasValidAuth по смещению +8. Пока байт нулевой, клиент уводит выполнение
# в ветку «аккаунт не допущен» и не пускает в чат.
#
# arm64 (Apple Silicon):
#     ldrb  w3, [x0, #8]          ; читает hasValidAuth
#     tbz   w3, #0, <skip>        ; флаг нулевой — прыгаем мимо успешной ветки
#     ...                        ; 1-2 инструкции контекста
#     stp   x3, x4, [x0, #0x60]   ; успешная ветка: запись токена
#
# Правка: mov w3,#1 + strb w3,[x0,#8] — флаг принудительно True, условный
# переход убран, выполнение всегда идёт по успешной ветке.
#
# x64:
#     cmp   byte [rax+8], 0
#     je    <skip>
#     mov   rdx, [rsp+disp8]
#     mov   [rax+0x60], rdx
#
# Правка: mov byte [rax+8],1 + nop + nop.

_ARM64_LDRB_W3_X0_8 = b"\x03\x20\x40\x39"          # ldrb w3, [x0, #8]
_ARM64_MOV_W3_1 = b"\x23\x00\x80\x52"               # mov  w3, #1
_ARM64_STRB_W3_X0_8 = b"\x03\x20\x00\x39"          # strb w3, [x0, #8]
# tbz w3, #bit, ±imm19: байт с маской регистра, затем 2 байта смещения (старший 0x36).
_ARM64_TBZ_W3_ANY_BIT = rb"[\x03\x23\x43\x63\x83\xa3\xc3\xe3]..\x36"
# 1-2 инструкции контекста, затем stp x3, x4, [x0, #0x60].
_ARM64_TOKEN_STORE = rb"(?:....){1,2}\x03\x10\x06\xa9"

_X64_CMP_BYTE_RAX8 = b"\x80\x78\x08\x00"            # cmp byte [rax+8], 0
_X64_JE = rb"\x74."
_X64_TAIL = rb"\x48\x8b.\x24.\x48\x89.\x60"         # mov rdx,[rsp+..] ; mov [rax+0x60],rdx
_X64_MOV_BYTE_RAX8_1 = b"\xc6\x40\x08\x01"          # mov byte [rax+8], 1
_X64_NOP2 = b"\x90\x90"


class ByteGate:
    """Один байтовый гейт: сигнатура «до» -> сигнатура «после».

    Фикс всегда той же длины, что и оригинал, поэтому правка выполняется
    in-place и не сдвигает остальной файл.

    resolve() возвращает (state, offsets):
        state = 'patched'   — сигнатура «после» найдена
        state = 'unpatched' — найдена сигнатура «до», нужен фикс
    Бросает LookupError, если сигнатура не найдена или (если
    allow_multiple=False) найдена в нескольких местах.
    """

    def __init__(self, label, unpatched, patched, fix, offset=0, allow_multiple=False,
                 arch="all", quick_check=None, anchor_unpatched=None, anchor_patched=None):
        self.label = label
        self.unpatched_re = re.compile(unpatched, re.S)
        self.patched_re = re.compile(patched, re.S)
        self.fix = fix
        self.offset = offset
        self.allow_multiple = allow_multiple
        self.arch = arch
        self.quick_check = tuple(quick_check) if quick_check else ()
        self.anchor_unpatched = anchor_unpatched
        self.anchor_patched = anchor_patched

    @property
    def desc(self):
        return self.label

    def resolve(self, data):
        if self.quick_check and not any(q in data for q in self.quick_check):
            raise LookupError(
                f"сигнатура гейта «{self.label}» не найдена (quick check)")

        if self.anchor_patched:
            patched_hits = []
            idx = 0
            while True:
                pos = data.find(self.anchor_patched, idx)
                if pos == -1:
                    break
                start = max(0, pos - self.offset)
                chunk = data[start:start + self.offset + len(self.fix) + 48]
                for m in self.patched_re.finditer(chunk):
                    hit = start + m.start() + self.offset
                    if hit not in patched_hits:
                        patched_hits.append(hit)
                idx = pos + 1
        else:
            patched_hits = [m.start() + self.offset for m in self.patched_re.finditer(data)]

        if patched_hits:
            return ("patched", sorted(patched_hits))

        if self.anchor_unpatched:
            hits = []
            idx = 0
            while True:
                pos = data.find(self.anchor_unpatched, idx)
                if pos == -1:
                    break
                start = max(0, pos - self.offset)
                chunk = data[start:start + self.offset + len(self.fix) + 48]
                for m in self.unpatched_re.finditer(chunk):
                    hit = start + m.start() + self.offset
                    if hit not in hits:
                        hits.append(hit)
                idx = pos + 1
        else:
            hits = [m.start() + self.offset for m in self.unpatched_re.finditer(data)]

        if not hits:
            raise LookupError(
                f"сигнатура гейта «{self.label}» не найдена (unsupported version?)")
        if len(hits) > 1 and not self.allow_multiple:
            raise LookupError(
                f"сигнатура гейта «{self.label}» неоднозначна ({len(hits)} совпадения) "
                "— отказ от угадывания")
        return ("unpatched", sorted(hits))

    def resolve_all(self, data):
        """Совместимость со старым MultiGate.resolve_all: [(kind, offsets, gate)]."""
        state, offsets = self.resolve(data)
        return [(state, offsets, self)]


class GateGroup:
    """Несколько вариантов одного логического гейта (по архитектуре/версии).

    Один бинарник может совпасть сразу с несколькими вариантами (например,
    arm64 1.2.7 содержит и инлайн-проверку w2, и проверку w4 в хелпере) —
    закрывать нужно все, иначе экран выживает.
    """

    def __init__(self, *gates, desc=""):
        self.gates = gates
        self.desc = desc

    def resolve_all(self, data):
        """Все совпавшие суб-гейты: [(kind, offsets, gate)].

        Бросает LookupError, если не совпал ни один вариант.
        """
        matches = []
        last_error = None
        for gate in self.gates:
            try:
                matches.extend(gate.resolve_all(data))
            except LookupError as e:
                last_error = e
        if not matches:
            raise last_error or LookupError("no gate signature matched")
        return matches


VALIDATOR_GATES = (
    ByteGate(
        "hasValidAuth=true (arm64)",
        _ARM64_LDRB_W3_X0_8 + _ARM64_TBZ_W3_ANY_BIT + _ARM64_TOKEN_STORE,
        _ARM64_MOV_W3_1 + _ARM64_STRB_W3_X0_8 + _ARM64_TOKEN_STORE,
        _ARM64_MOV_W3_1 + _ARM64_STRB_W3_X0_8,
        arch="arm64",
        quick_check=(_ARM64_LDRB_W3_X0_8, _ARM64_STRB_W3_X0_8),
    ),
    ByteGate(
        "hasValidAuth=true (x64)",
        _X64_CMP_BYTE_RAX8 + _X64_JE + _X64_TAIL,
        _X64_MOV_BYTE_RAX8_1 + _X64_NOP2 + _X64_TAIL,
        _X64_MOV_BYTE_RAX8_1 + _X64_NOP2,
        arch="x64",
        quick_check=(_X64_CMP_BYTE_RAX8, _X64_MOV_BYTE_RAX8_1),
    ),
)


def find_validator_gate(data):
    """Совместимость со старым API: (state, offset, label, fix_bytes)."""
    target_arch = detect_binary_arch(data)
    for gate in VALIDATOR_GATES:
        if target_arch not in ("unknown", "universal") and gate.arch != "all" and gate.arch != target_arch:
            continue
        try:
            state, offsets = gate.resolve(data)
        except LookupError:
            continue
        return (state, offsets[0], gate.label, gate.fix)
    raise LookupError("сигнатура гейта hasValidAuth не найдена (unsupported version?)")


def validator_gate_state(data):
    """'patched' | 'unpatched' | 'unknown' — для показа статуса."""
    try:
        return find_validator_gate(data)[0]
    except LookupError:
        return "unknown"


# --------------------------------------------------------------------------- #
#  Гейты экрана допустимости CLI agy                                           #
# --------------------------------------------------------------------------- #

# Экран «Eligibility check failed:» в CLI agy. Сигнатуры различаются по
# архитектуре и по версии сборки; x64 и arm64 никогда не пересекаются,
# но один бинарник может содержать сразу два arm64-гейта (инлайн-проверка
# в функции экрана + проверка в хелпере) — патчить нужно все.
#
# x64:
#     test rax,rax ; je eligible ; cmp byte[rax+8],0 ; jne eligible ; call failure_builder
# Повторная проверка rax держит ZF=0, поэтому jne всегда выбирает eligible.
#
# arm64 (классика):
#     cbnz x1,error ; cbz x0,eligible ; ldrb w1,[x0,#8] ; tbnz w1,#0,eligible
# Загрузка 1 вместо чтения байта заставляет tbnz всегда выбирать eligible.
#
# arm64 1.2.7+ (флаг в w4):
#     cbz x0,eligible ; ldrb w4,[x0,#8] ; tbnz w4,#0,eligible
#
# arm64 1.2.7+ (инлайн, флаг в w2):
#     cbnz x1,? ; cbz x0,eligible ; ldrb w2,[x0,#8] ; tbnz w2,#0,eligible

CLI_GATE_X64 = ByteGate(
    "eligibility screen off (x64)",
    rb"\x48\x85\xc0\x0f\x84....\x80\x78\x08\x00\x0f\x85....",
    rb"\x48\x85\xc0\x0f\x84....\x48\x85\xc0\x90\x0f\x85....",
    b"\x48\x85\xc0\x90",
    offset=9,
    allow_multiple=True,
    arch="x64",
    quick_check=(b"\x80\x78\x08\x00\x0f\x85", b"\x48\x85\xc0\x90\x0f\x85"),
)

CLI_GATE_ARM64 = ByteGate(
    "eligibility screen off (arm64)",
    rb"...\xb5...\xb4\x01\x20\x40\x39[\x01\x21\x41\x61\x81\xa1\xc1\xe1].[\x00-\x07]\x37"
    rb"...[\x94-\x97](?:\xe0\x4b\x00\xf9\xe1\x33\x00\xf9\xe2\x43\x00\xf9"
    rb"|\xe0\x4f\x00\xf9\xe1\x33\x00\xf9\xe2\x47\x00\xf9\xe3\x2f\x00\xf9)",
    rb"...\xb5...\xb4\x21\x00\x80\x52[\x01\x21\x41\x61\x81\xa1\xc1\xe1].[\x00-\x07]\x37"
    rb"...[\x94-\x97](?:\xe0\x4b\x00\xf9\xe1\x33\x00\xf9\xe2\x43\x00\xf9"
    rb"|\xe0\x4f\x00\xf9\xe1\x33\x00\xf9\xe2\x47\x00\xf9\xe3\x2f\x00\xf9)",
    b"\x21\x00\x80\x52",
    offset=8,
    allow_multiple=True,
    arch="arm64",
    quick_check=(b"\x01\x20\x40\x39", b"\x21\x00\x80\x52"),
    anchor_unpatched=b"\x01\x20\x40\x39",
    anchor_patched=b"\x21\x00\x80\x52",
)

CLI_GATE_ARM64_W4 = ByteGate(
    "eligibility screen off (arm64, w4)",
    rb"[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4\x04\x20\x40\x39"
    rb"[\x04\x24\x44\x64\x84\xa4\xc4\xe4].[\x00-\x07]\x37\x04\x94\x44\xa9",
    rb"[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4\x24\x00\x80\x52"
    rb"[\x04\x24\x44\x64\x84\xa4\xc4\xe4].[\x00-\x07]\x37\x04\x94\x44\xa9",
    b"\x24\x00\x80\x52",
    offset=4,
    allow_multiple=True,
    arch="arm64",
    quick_check=(b"\x04\x20\x40\x39", b"\x24\x00\x80\x52"),
    anchor_unpatched=b"\x04\x20\x40\x39",
    anchor_patched=b"\x24\x00\x80\x52",
)

CLI_GATE_ARM64_W2 = ByteGate(
    "eligibility screen off (arm64, w2)",
    rb"[\x01\x21\x41\x61\x81\xa1\xc1\xe1]..\xb5[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4"
    rb"\x02\x20\x40\x39[\x02\x22\x42\x62\x82\xa2\xc2\xe2].[\x00-\x07]\x37...[\x94-\x97]",
    rb"[\x01\x21\x41\x61\x81\xa1\xc1\xe1]..\xb5[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4"
    rb"\x22\x00\x80\x52[\x02\x22\x42\x62\x82\xa2\xc2\xe2].[\x00-\x07]\x37...[\x94-\x97]",
    b"\x22\x00\x80\x52",
    offset=8,
    allow_multiple=True,
    arch="arm64",
    quick_check=(b"\x02\x20\x40\x39", b"\x22\x00\x80\x52"),
    anchor_unpatched=b"\x02\x20\x40\x39",
    anchor_patched=b"\x22\x00\x80\x52",
)

CLI_GATES = (CLI_GATE_X64, CLI_GATE_ARM64, CLI_GATE_ARM64_W2, CLI_GATE_ARM64_W4)

# Группа всех вариантов экрана допустимости — для совместимости со старым
# API patcher.agy.patcher.ALL_GATES (им пользуется test-agy/test_cli_releases.py).
CLI_GATE_GROUP = GateGroup(
    CLI_GATE_X64, CLI_GATE_ARM64, CLI_GATE_ARM64_W2, CLI_GATE_ARM64_W4,
    desc="eligibility screen off",
)


# --------------------------------------------------------------------------- #
#  JS-патчи Antigravity IDE                                                    #
# --------------------------------------------------------------------------- #

# Гейт допустимости в Antigravity IDE переехал в confirmUserForService:
#   const s = !!r || !i;      ; r — наличие free-tier в allowedTiers,
#                              ; i — причина отказа из ineligibleTiers
#   ... if (s) { await onboardUser("free-tier", t); ... }
#
# Правка 1: s = true — клиент перестаёт проверять допуск к free-tier.
# Правка 2: onboardUser("free-tier") -> onboardUser("standard-tier") — сервер
# отклоняет онбординг в free-tier для аккаунтов вне поддерживаемых регионов,
# а standard-tier принимает.
#
# Для IDE 2.5.x остаётся запасной вариант — «внутренняя» ветка входа.
IDE_TIER_GATE_RE = re.compile(
    rb"(const [A-Za-z_$0-9]+=)!![A-Za-z_$0-9]+\|\|![A-Za-z_$0-9]+([,;])")
IDE_TIER_GATE_FIX = rb"\1!0/*ag_gate*/\2"

IDE_TIER_ONBOARD_RE = re.compile(rb'onboardUser\("free-tier",t\)')
IDE_TIER_ONBOARD_FIX = b'onboardUser("standard-tier",t)/*ag_tier*/'

IDE_LEGACY_RE = re.compile(
    rb"(resetIsTierGCPTos\(\),)this\.[A-Za-z_$0-9]+\.isGoogleInternal")
IDE_LEGACY_FIX = b"resetIsTierGCPTos(),true"

# Маркеры уже применённых правок.
IDE_PATCH_MARKERS = (b"/*ag_gate*/", b"/*ag_tier*/", b"resetIsTierGCPTos(),true")

# Признаки чужой правки main.js (сторонний «разблокировщик»): подменённый блок
# авторизации. Проверяем характерные вставки, а не штатные вызовы вроде
# onboardUser("free-tier") — они есть и в оригинале.
IDE_CUSTOM_MARKERS = (
    b"AG_PROXY_HOOK",
    b'userTier: { id: "pro"',
    b"__res = { settings:",
    b"catch(_) { try { await this._cloudCodeMainService.loadCodeAssist",
)


# --------------------------------------------------------------------------- #
#  План правок                                                                 #
# --------------------------------------------------------------------------- #

class PatchEdit:
    """Одна правка: байты до/после по фиксированному смещению."""

    __slots__ = ("offset", "before", "after", "label")

    def __init__(self, offset, before, after, label):
        self.offset = offset
        self.before = bytes(before)
        self.after = bytes(after)
        self.label = label

    def __repr__(self):
        return (f"PatchEdit(0x{self.offset:x}, {self.label}, "
                f"{len(self.before)}->{len(self.after)})")


def plan_go_patches(data, cli_screens=False, revert_proxy=True):
    """План правок для Go-бинарника (language_server / agy).

    data — bytes файла. Возвращает (edits, states):
        edits  — список PatchEdit с правками;
        states — список (label, state) по гейтам для показа в интерфейсе.

    Сигнатуры, которые не найдены, просто не попадают в states — это не
    ошибка: в одном бинарнике может быть не весь набор гейтов.
    """
    edits = []
    states = []
    target_arch = detect_binary_arch(data)

    # --- гейт валидатора ---
    validator_state = None
    for gate in VALIDATOR_GATES:
        if target_arch not in ("unknown", "universal") and gate.arch != "all" and gate.arch != target_arch:
            continue
        try:
            state, offsets = gate.resolve(data)
        except LookupError:
            continue
        validator_state = state
        states.append((gate.label, state))
        if state == "unpatched":
            for off in offsets:
                before = data[off:off + len(gate.fix)]
                edits.append(PatchEdit(off, before, gate.fix,
                                        f"gate: {gate.label}"))
        break
    if validator_state is None:
        states.append(("hasValidAuth", "unknown"))

    # --- экраны допустимости CLI (только для agy) ---
    if cli_screens:
        for gate in CLI_GATES:
            if target_arch not in ("unknown", "universal") and gate.arch != "all" and gate.arch != target_arch:
                continue
            try:
                state, offsets = gate.resolve(data)
            except LookupError:
                continue
            states.append((gate.label, state))
            if state == "unpatched":
                for off in offsets:
                    before = data[off:off + len(gate.fix)]
                    edits.append(PatchEdit(off, before, gate.fix,
                                            f"gate: {gate.label}"))

    # --- откат подмены прокси ---
    if revert_proxy:
        for off, original in find_proxy_reverts(data):
            states.append(("proxy https_proxy", "unpatched"))
            edits.append(PatchEdit(off, PROXY_INJECTED, original,
                                    "revert: AG_LS_PROXY -> https_proxy"))

    return edits, states


def apply_patches_to_data(data, patches):
    """Применяет список (offset, gate) к байтовому массиву in-place."""
    buf = bytearray(data)
    for off, gate in patches:
        buf[off:off + len(gate.fix)] = gate.fix
    return bytes(buf)


def apply_edits(data, edits):
    """Применяет список PatchEdit к bytes. Правки — с конца к началу.

    Каждая правка проверяется: на месте должны лежать ровно те байты,
    которые видели при планировании. Иначе — отказ (файл меняли извне).
    """
    buf = bytearray(data)
    for edit in sorted(edits, key=lambda e: e.offset, reverse=True):
        current = bytes(buf[edit.offset:edit.offset + len(edit.before)])
        if current != edit.before:
            raise ValueError(
                f"байты по смещению 0x{edit.offset:x} изменились извне — правка отменена")
        buf[edit.offset:edit.offset + len(edit.before)] = edit.after
    return bytes(buf)


def plan_ide_js(data):
    """План правок для main.js Antigravity IDE.

    Возвращает (edits, states). edits — список кортежей (before, after, label)
    для замены подстроки (в отличие от байтовых патчей, длина тут меняется).

    Правила:
      * если уже стоят наши маркеры — ничего не делаем (идемпотентность);
      * если найден тир-гейт 2.6.x — патчим его и онбординг, legacy не трогаем;
      * иначе, если найден legacy-гейт 2.5.x — патчим только его;
      * если сигнатура неоднозначна — отказ.
    """
    edits = []
    states = []

    if any(marker in data for marker in IDE_PATCH_MARKERS):
        states.append(("tier gate (2.6.x)", "patched"))
        return edits, states

    tier_hits = list(IDE_TIER_GATE_RE.finditer(data))
    onboard_hits = list(IDE_TIER_ONBOARD_RE.finditer(data))
    legacy_hits = list(IDE_LEGACY_RE.finditer(data))

    if tier_hits:
        if len(tier_hits) > 1:
            raise LookupError(
                f"сигнатура тир-гейта неоднозначна ({len(tier_hits)} совпадения) "
                "— отказ от угадывания")
        match = tier_hits[0]
        before = match.group(0)
        after = IDE_TIER_GATE_RE.sub(IDE_TIER_GATE_FIX, before, count=1)
        edits.append((before, after, "gate: tier eligibility -> true"))
        states.append(("tier gate (2.6.x)", "unpatched"))

        if len(onboard_hits) > 1:
            raise LookupError(
                f"сигнатура онбординга неоднозначна ({len(onboard_hits)} совпадения) "
                "— отказ от угадывания")
        if onboard_hits:
            before = onboard_hits[0].group(0)
            edits.append((before, IDE_TIER_ONBOARD_FIX,
                          "tier: onboardUser free-tier -> standard-tier"))
            states.append(("tier onboarding", "unpatched"))
        else:
            states.append(("tier onboarding", "not-found"))
        return edits, states

    if legacy_hits:
        if len(legacy_hits) > 1:
            raise LookupError(
                f"сигнатура legacy-гейта неоднозначна ({len(legacy_hits)} совпадения) "
                "— отказ от угадывания")
        before = legacy_hits[0].group(0)
        edits.append((before, IDE_LEGACY_FIX,
                      "gate: resetIsTierGCPTos() -> true"))
        states.append(("legacy gate (2.5.x)", "unpatched"))
        return edits, states

    states.append(("auth gate", "not-found"))
    return edits, states


def ide_js_state(data):
    """'patched' | 'unpatched' | 'unknown' — статус JS-гейта для панели."""
    if any(marker in data for marker in IDE_PATCH_MARKERS):
        return "patched"
    if IDE_TIER_GATE_RE.search(data) or IDE_LEGACY_RE.search(data):
        return "unpatched"
    return "unknown"
