#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ag_patcher — патчер Google Antigravity для macOS (Apple Silicon).

Зачем:
  * Antigravity 2.x / Antigravity IDE прячут клиентский гейт региональной
    допустимости аккаунта. Он снимается локальной правкой — сеть трогать не нужно.
  * После правки бинарника ломается codesign-подпись бандла, и macOS начинает
    считать приложение повреждённым («улетает в корзину»). Поэтому патч всегда
    завершается переподписью бандла (ad-hoc, с сохранением entitlements).
  * Частые «анлокеры» (в т.ч. с Telegram-ключом) вшивают в бинарник подмену
    переменной прокси (AG_LS_PROXY вместо https_proxy). Если их локальный сервис
    выключен, приложение шлёт запросы в никуда и выдаёт «There was a network
    issue…». Здесь такой след откатывается.

Что делает:
  1. manager — гейт hasValidAuth в Go-бэкенде Antigravity.app (arm64/x64).
  2. ide     — гейт авторизации в Antigravity IDE (resources/app/out/main.js).
  3. cli     — тот же гейт в CLI agy (~/.local/bin/agy), если он есть.
  4. Переподписывает .app изнутри наружу и снимает карантин.
  5. Ведёт журнал правок в байтах рядом с файлом — откат точный и не требует
     копии 180 МБ.

Только локальные правки. Никаких сторонних сервисов, никакого перехвата TLS.

Использование:
    python3 ag_patcher.py status
    python3 ag_patcher.py patch --target all
    python3 ag_patcher.py restore --target all
    python3 ag_patcher.py doctor
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.4.0"
JOURNAL_SUFFIX = ".agpatch.json"
# Журналы держим ВНЕ бандлов: любой новый файл внутри .app после подписи
# ломает codesign-печать, и macOS считает приложение повреждённым.
JOURNAL_ROOT = Path.home() / "Library" / "Application Support" / "ag_patcher" / "journals"

# --------------------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------------------

_C = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "cyan": "\033[36m", "dim": "\033[2m", "bold": "\033[1m", "reset": "\033[0m",
}


def c(text: str, *styles: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(_C[s] for s in styles) + text + _C["reset"]


def info(msg: str) -> None:
    print(c("•", "cyan"), msg)


def ok(msg: str) -> None:
    print(c("✓", "green"), msg)


def warn(msg: str) -> None:
    print(c("!", "yellow"), msg)


def err(msg: str) -> None:
    print(c("✗", "red"), msg)


def head(msg: str) -> None:
    print()
    print(c(msg, "bold"))
    print(c("─" * max(len(msg), 40), "dim"))


# --------------------------------------------------------------------------------------
# Утилиты
# --------------------------------------------------------------------------------------


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bytes(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def write_in_place(path: Path, data: bytes) -> None:
    """Пишет поверх файла, сохраняя inode (важно для .app: не плодим копии)."""
    with open(path, "r+b") as fh:
        fh.seek(0)
        fh.write(data)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def clear_flags(path: Path) -> None:
    """Снимает uchg/uchg-подобные флаги, которые мешают записи внутрь .app."""
    if sys.platform != "darwin":
        return
    try:
        run(["chflags", "-R", "nouchg", str(path)])
    except OSError:
        pass


def strip_quarantine(path: Path) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        proc = run(["xattr", "-dr", "com.apple.quarantine", str(path)])
        return proc.returncode == 0
    except OSError:
        return False


def has_quarantine(path: Path) -> bool:
    if sys.platform != "darwin":
        return False
    proc = run(["xattr", "-p", "com.apple.quarantine", str(path)])
    return proc.returncode == 0


# --------------------------------------------------------------------------------------
# Журнал правок (откат по байтам)
# --------------------------------------------------------------------------------------


@dataclass
class Edit:
    offset: int
    before: bytes
    after: bytes
    label: str

    def to_json(self) -> dict:
        return {
            "offset": self.offset,
            "before": self.before.hex(),
            "after": self.after.hex(),
            "label": self.label,
        }

    @staticmethod
    def from_json(raw: dict) -> "Edit":
        return Edit(
            offset=int(raw["offset"]),
            before=bytes.fromhex(raw["before"]),
            after=bytes.fromhex(raw["after"]),
            label=raw.get("label", ""),
        )


@dataclass
class Journal:
    path: Path
    target: str
    sha256_before: str = ""
    sha256_after: str = ""
    rebuilt_from: str = ""
    edits: list[Edit] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, target: str) -> "Journal":
        if path.exists():
            try:
                raw = json.loads(path.read_text("utf-8"))
                return cls(
                    path=path,
                    target=raw.get("target", target),
                    sha256_before=raw.get("sha256_before", ""),
                    sha256_after=raw.get("sha256_after", ""),
                    rebuilt_from=raw.get("rebuilt_from", ""),
                    edits=[Edit.from_json(e) for e in raw.get("edits", [])],
                )
            except (OSError, ValueError, KeyError):
                pass
        return cls(path=path, target=target)

    def add(self, offset: int, before: bytes, after: bytes, label: str) -> None:
        self.edits.append(Edit(offset, before, after, label))

    def save(self) -> None:
        payload = {
            "version": 1,
            "tool_version": VERSION,
            "target": self.target,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sha256_before": self.sha256_before,
            "sha256_after": self.sha256_after,
            "rebuilt_from": self.rebuilt_from,
            "edits": [e.to_json() for e in self.edits],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")


# --------------------------------------------------------------------------------------
# Сигнатуры гейта
# --------------------------------------------------------------------------------------
#
# Гейт в Go-бэкенде: authclient.(*PersonalAuthValidator).Validate кладёт в AuthResult
# байт hasValidAuth по смещению +8. Пока байт нулевой, клиент уводит выполнение в ветку
# «аккаунт не допущен» и не пускает в чат.
#
# arm64 (Apple Silicon):
#     ldrb  w3, [x0, #8]          ; читает hasValidAuth
#     tbz   w3, #0, <skip>        ; если флаг нулевой — прыгаем мимо успешной ветки
#     ...                        ; 1–2 инструкции контекста
#     stp   x3, x4, [x0, #0x60]   ; успешная ветка: запись токена
# Правка: mov w3,#1 ; strb w3,[x0,#8] — флаг принудительно True, переход убран,
# выполнение всегда идёт по успешной ветке.
#
# x64:
#     cmp   byte [rax+8], 0
#     je    <skip>
#     mov   rdx, [rsp+disp8]
#     mov   [rax+0x60], rdx
# Правка: mov byte [rax+8],1 ; nop ; nop — флаг True, переход убран.

_ARM64_LDRB_W3_X0_8 = b"\x03\x20\x40\x39"          # ldrb w3, [x0, #8]
_ARM64_MOV_W3_1 = b"\x23\x00\x80\x52"               # mov  w3, #1
_ARM64_STRB_W3_X0_8 = b"\x03\x20\x00\x39"          # strb w3, [x0, #8]
# tbz w3, #bit, ±imm19: байт с маской регистра, затем 2 байта смещения (старший 0x36).
_ARM64_TBZ_W3_ANY_BIT = rb"[\x03\x23\x43\x63\x83\xa3\xc3\xe3]..\x36"
# 1–2 инструкции контекста, затем stp x3, x4, [x0, #0x60].
_ARM64_TOKEN_STORE = rb"(?:....){1,2}\x03\x10\x06\xa9"

_X64_CMP_BYTE_RAX8 = b"\x80\x78\x08\x00"            # cmp byte [rax+8], 0
_X64_JE = rb"\x74."
_X64_TAIL = rb"\x48\x8b.\x24.\x48\x89.\x60"         # mov rdx,[rsp+..] ; mov [rax+0x60],rdx
_X64_MOV_BYTE_RAX8_1 = b"\xc6\x40\x08\x01"          # mov byte [rax+8], 1
_X64_NOP2 = b"\x90\x90"


@dataclass
class Gate:
    label: str
    unpatched: re.Pattern
    patched: re.Pattern
    fix: bytes
    offset: int = 0
    allow_multiple: bool = False

    def locate(self, data: bytes) -> tuple[str, int]:
        """-> (state, offset). state: patched | unpatched"""
        hit = self.patched.search(data)
        if hit:
            return "patched", hit.start() + self.offset
        hit = self.unpatched.search(data)
        if not hit:
            raise LookupError(f"сигнатура гейта «{self.label}» не найдена")
        if self.unpatched.search(data, hit.end()) and not self.allow_multiple:
            raise LookupError(f"сигнатура гейта «{self.label}» неоднозначна — отказ от угадывания")
        return "unpatched", hit.start() + self.offset

    def resolve(self, data: bytes) -> tuple[str, list[int]]:
        """-> (state, offsets). state: patched | unpatched"""
        patched_hits = [m.start() + self.offset for m in self.patched.finditer(data)]
        if patched_hits:
            return "patched", patched_hits
        hits = [m.start() + self.offset for m in self.unpatched.finditer(data)]
        if not hits:
            raise LookupError(f"сигнатура гейта «{self.label}» не найдена")
        if len(hits) > 1 and not self.allow_multiple:
            raise LookupError(f"сигнатура гейта «{self.label}» неоднозначна ({len(hits)} совпадения)")
        return "unpatched", hits


GATES: list[Gate] = [
    Gate(
        label="hasValidAuth (arm64)",
        unpatched=re.compile(
            _ARM64_LDRB_W3_X0_8 + _ARM64_TBZ_W3_ANY_BIT + _ARM64_TOKEN_STORE, re.S
        ),
        patched=re.compile(
            _ARM64_MOV_W3_1 + _ARM64_STRB_W3_X0_8 + _ARM64_TOKEN_STORE, re.S
        ),
        fix=_ARM64_MOV_W3_1 + _ARM64_STRB_W3_X0_8,
    ),
    Gate(
        label="hasValidAuth (x64)",
        unpatched=re.compile(
            _X64_CMP_BYTE_RAX8 + _X64_JE + _X64_TAIL, re.S
        ),
        patched=re.compile(
            _X64_MOV_BYTE_RAX8_1 + _X64_NOP2 + _X64_TAIL, re.S
        ),
        fix=_X64_MOV_BYTE_RAX8_1 + _X64_NOP2,
    ),
]

CLI_GATES: list[Gate] = [
    Gate(
        label="eligibility screen off (x64)",
        unpatched=re.compile(
            rb"\x48\x85\xc0\x0f\x84....\x80\x78\x08\x00\x0f\x85....", re.S
        ),
        patched=re.compile(
            rb"\x48\x85\xc0\x0f\x84....\x48\x85\xc0\x90\x0f\x85....", re.S
        ),
        fix=b"\x48\x85\xc0\x90",
        offset=9,
        allow_multiple=True,
    ),
    Gate(
        label="eligibility screen off (arm64)",
        unpatched=re.compile(
            rb"...\xb5...\xb4\x01\x20\x40\x39[\x01\x21\x41\x61\x81\xa1\xc1\xe1].[\x00-\x07]\x37"
            rb"...[\x94-\x97](?:\xe0\x4b\x00\xf9\xe1\x33\x00\xf9\xe2\x43\x00\xf9"
            rb"|\xe0\x4f\x00\xf9\xe1\x33\x00\xf9\xe2\x47\x00\xf9\xe3\x2f\x00\xf9)", re.S
        ),
        patched=re.compile(
            rb"...\xb5...\xb4\x21\x00\x80\x52[\x01\x21\x41\x61\x81\xa1\xc1\xe1].[\x00-\x07]\x37"
            rb"...[\x94-\x97](?:\xe0\x4b\x00\xf9\xe1\x33\x00\xf9\xe2\x43\x00\xf9"
            rb"|\xe0\x4f\x00\xf9\xe1\x33\x00\xf9\xe2\x47\x00\xf9\xe3\x2f\x00\xf9)", re.S
        ),
        fix=b"\x21\x00\x80\x52",
        offset=8,
        allow_multiple=True,
    ),
    Gate(
        label="eligibility screen off (arm64, w4)",
        unpatched=re.compile(
            rb"[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4\x04\x20\x40\x39"
            rb"[\x04\x24\x44\x64\x84\xa4\xc4\xe4].[\x00-\x07]\x37\x04\x94\x44\xa9", re.S
        ),
        patched=re.compile(
            rb"[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4\x24\x00\x80\x52"
            rb"[\x04\x24\x44\x64\x84\xa4\xc4\xe4].[\x00-\x07]\x37\x04\x94\x44\xa9", re.S
        ),
        fix=b"\x24\x00\x80\x52",
        offset=4,
        allow_multiple=True,
    ),
    Gate(
        label="eligibility screen off (arm64, w2)",
        unpatched=re.compile(
            rb"[\x01\x21\x41\x61\x81\xa1\xc1\xe1]..\xb5[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4"
            rb"\x02\x20\x40\x39[\x02\x22\x42\x62\x82\xa2\xc2\xe2].[\x00-\x07]\x37...[\x94-\x97]", re.S
        ),
        patched=re.compile(
            rb"[\x01\x21\x41\x61\x81\xa1\xc1\xe1]..\xb5[\x00\x20\x40\x60\x80\xa0\xc0\xe0]..\xb4"
            rb"\x22\x00\x80\x52[\x02\x22\x42\x62\x82\xa2\xc2\xe2].[\x00-\x07]\x37...[\x94-\x97]", re.S
        ),
        fix=b"\x22\x00\x80\x52",
        offset=8,
        allow_multiple=True,
    ),
]

# След чужого «анлокера»: https_proxy (11 байт) → AG_LS_PROXY (11 байт).
PROXY_ORIGINAL = b"https_proxy"
PROXY_INJECTED = b"AG_LS_PROXY"


def find_gate(data: bytes) -> tuple[Gate, str, int]:
    last_error: Exception | None = None
    for gate in GATES:
        try:
            state, offset = gate.locate(data)
            return gate, state, offset
        except LookupError as exc:
            last_error = exc
    raise LookupError(str(last_error) if last_error else "гейт не найден")


# --------------------------------------------------------------------------------------
# JS-патч IDE
# --------------------------------------------------------------------------------------
#
# Antigravity IDE 2.6.x: регион проверяется в confirmUserForService.
#   const s = !!r || !i;                 -> s = «аккаунту доступен free-tier»
#   ... if (s) { await onboardUser("free-tier", t) ... }
# Гейт снимается принудительным true, а онбординг переводится на тир standard-tier:
# сервер принимает его для аккаунтов, которым free-tier недоступен по региону.
#
# Antigravity IDE 2.5.x: проверка была в ветке «внутреннего» входа —
# resetIsTierGCPTos(),this.<x>.isGoogleInternal. Поддерживается как запасной вариант.

IDE_TIER_GATE_RE = re.compile(
    rb"(const [A-Za-z_$0-9]+=)!![A-Za-z_$0-9]+\|\|![A-Za-z_$0-9]+([,;])"
)
IDE_TIER_GATE_FIX = rb"\1!0/*ag_gate*/\2"

IDE_TIER_ONBOARD_RE = re.compile(rb'onboardUser\("free-tier",t\)')
IDE_TIER_ONBOARD_FIX = b'onboardUser("standard-tier",t)/*ag_tier*/'

IDE_AUTH_RE = re.compile(
    rb"(resetIsTierGCPTos\(\),)this\.[A-Za-z_$0-9]+\.isGoogleInternal"
)
IDE_AUTH_FIX = b"resetIsTierGCPTos(),true"

# Маркеры уже применённых правок.
IDE_PATCH_MARKERS = (b"/*ag_gate*/", b"/*ag_tier*/", b"resetIsTierGCPTos(),true")

# Признаки чужой правки main.js: подменённый блок авторизации.
# Проверяем именно характерные вставки, а не штатные вызовы вроде
# onboardUser("free-tier") — они есть и в оригинале.
IDE_CUSTOM_MARKERS = (
    b"AG_PROXY_HOOK",
    b'userTier: { id: "pro"',
    b"__res = { settings:",
    b"catch(_) { try { await this._cloudCodeMainService.loadCodeAssist",
)


def ide_cache_dirs() -> list[Path]:
    home = Path.home()
    base = home / "Library" / "Application Support" / "Antigravity IDE"
    return [base / "CachedData", base / "Code Cache" / "js"]


# --------------------------------------------------------------------------------------
# Обнаружение целей
# --------------------------------------------------------------------------------------

BUNDLE_SEARCH_DIRS = [Path("/Applications"), Path.home() / "Applications"]


def find_bundle(*names: str) -> Path | None:
    for directory in BUNDLE_SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_dir():
                return candidate
    return None


def bundle_version(app: Path) -> str:
    plist = app / "Contents" / "Info.plist"
    try:
        with open(plist, "rb") as fh:
            info_plist = plistlib.load(fh)
        return str(info_plist.get("CFBundleShortVersionString", "?"))
    except (OSError, plistlib.InvalidFileException):
        return "?"


@dataclass
class GoTarget:
    """Go-бинарник с гейтом hasValidAuth."""
    name: str
    path: Path
    app: Path | None = None


@dataclass
class JsTarget:
    """JS-бандл IDE с гейтом авторизации."""
    name: str
    path: Path
    app: Path


@dataclass
class Target:
    key: str
    title: str
    app: Path | None
    go: list[GoTarget] = field(default_factory=list)
    js: list[JsTarget] = field(default_factory=list)


def discover() -> dict[str, Target]:
    targets: dict[str, Target] = {}

    manager_app = find_bundle("Antigravity.app")
    if manager_app:
        server = manager_app / "Contents" / "Resources" / "bin" / "language_server"
        go = [GoTarget("manager", server, manager_app)] if server.is_file() else []
        targets["manager"] = Target("manager", "Antigravity (app)", manager_app, go=go)

    ide_app = find_bundle("Antigravity IDE.app")
    if ide_app:
        main_js = ide_app / "Contents" / "Resources" / "app" / "out" / "main.js"
        js = [JsTarget("ide", main_js, ide_app)] if main_js.is_file() else []
        targets["ide"] = Target("ide", "Antigravity IDE", ide_app, js=js)

    cli = Path.home() / ".local" / "bin" / "agy"
    if cli.is_file():
        targets["cli"] = Target("cli", "Antigravity CLI (agy)", None, go=[GoTarget("cli", cli)])

    # Бэкенд Antigravity IDE: расширение само скачивает сюда agy и запускает
    # «agy --hub». Отдельный бинарник, отдельный гейт — патчить нужно и его.
    hub = Path.home() / ".gemini" / "bin" / "agy"
    if hub.is_file():
        targets["hub"] = Target("hub", "Antigravity IDE backend (agy --hub)", None,
                                go=[GoTarget("hub", hub)])

    return targets


# --------------------------------------------------------------------------------------
# Подпись
# --------------------------------------------------------------------------------------

MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # 64-bit LE
    b"\xce\xfa\xed\xfe",  # 32-bit LE
    b"\xca\xfe\xba\xbe",  # fat
    b"\xbe\xba\xfe\xca",  # fat (BE marker)
    b"\xfe\xed\xfa\xce",  # 32-bit BE
    b"\xfe\xed\xfa\xcf",  # 64-bit BE
}

BUNDLE_SUFFIXES = (".app", ".framework", ".xpc", ".bundle", ".plugin", ".appex")


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in MACHO_MAGICS
    except OSError:
        return False


def collect_signables(app: Path) -> list[Path]:
    """Все объекты для переподписи, от вложенных к верхнему (глубже — раньше)."""
    items: list[Path] = []
    for root, dirs, files in os.walk(app):
        for name in list(dirs):
            if name.endswith(BUNDLE_SUFFIXES):
                items.append(Path(root) / name)
        for name in files:
            candidate = Path(root) / name
            if candidate.is_file() and not candidate.is_symlink() and is_macho(candidate):
                items.append(candidate)
    items.sort(key=lambda p: len(p.parts), reverse=True)
    return items


def extract_entitlements(app: Path) -> dict:
    """Читает entitlements из текущей подписи бандла (даже если печать уже broken)."""
    proc = run(["codesign", "-d", "--entitlements", "-", "--xml", str(app)])
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    start = raw.find("<?xml")
    if start < 0:
        start = raw.find("<plist")
    if start < 0:
        return {}
    try:
        return plistlib.loads(raw[start:].encode("utf-8"))
    except Exception:
        return {}


def make_writable(path: Path) -> None:
    """Внутри Electron-фреймворка встречаются dylib с режимом 444 — codesign
    не может записать новую подпись, пока нет права на запись."""
    try:
        mode = path.stat().st_mode
        if not mode & 0o200:
            os.chmod(path, mode | 0o200)
    except OSError:
        pass


def resign_bundle(app: Path) -> tuple[bool, str]:
    """Переподписывает .app ad-hoc, сохраняя entitlements, и снимает карантин."""
    entitlements = extract_entitlements(app)
    entitlements.setdefault("com.apple.security.cs.disable-library-validation", True)
    entitlements.setdefault("com.apple.security.cs.allow-jit", True)

    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as tmp:
        plistlib.dump(entitlements, tmp)
        ent_path = Path(tmp.name)

    try:
        clear_flags(app)
        items = collect_signables(app)
        failures: list[str] = []
        for item in items:
            make_writable(item)
            if item.is_dir():
                make_writable(item)
            proc = run([
                "codesign", "--force", "--sign", "-", "--options", "runtime",
                "--timestamp=none", "--entitlements", str(ent_path), str(item),
            ])
            if proc.returncode != 0:
                last = (proc.stderr or "").strip().splitlines()
                failures.append(f"{item.name}: {last[-1] if last else 'ошибка codesign'}")
        make_writable(app)
        proc = run([
            "codesign", "--force", "--sign", "-", "--options", "runtime",
            "--timestamp=none", "--entitlements", str(ent_path), str(app),
        ])
        if proc.returncode != 0:
            failures.append(f"{app}: {(proc.stderr or '').strip()}")
        strip_quarantine(app)
        verify = run(["codesign", "--verify", "--deep", "--strict", str(app)])
        if verify.returncode != 0:
            failures.append(f"проверка подписи: {(verify.stderr or '').strip()}")
        if failures:
            return False, "; ".join(failures[:3])
        return True, f"подписано объектов: {len(items) + 1}"
    finally:
        try:
            ent_path.unlink()
        except OSError:
            pass


def resign_macho(path: Path) -> tuple[bool, str]:
    """Ad-hoc подпись одиночного Mach-O (CLI agy) с сохранением entitlements."""
    entitlements = extract_entitlements(path)
    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as tmp:
        plistlib.dump(entitlements, tmp)
        ent_path = Path(tmp.name)
    try:
        make_writable(path)
        proc = run([
            "codesign", "--force", "--sign", "-", "--timestamp=none",
            "--preserve-metadata=flags",
            "--entitlements", str(ent_path), str(path),
        ])
        if proc.returncode != 0:
            return False, (proc.stderr or "").strip()
        verify = run(["codesign", "--verify", "--strict", str(path)])
        if verify.returncode != 0:
            return False, (verify.stderr or "").strip()
        return True, "подпись обновлена"
    finally:
        try:
            ent_path.unlink()
        except OSError:
            pass


def signature_state(app: Path) -> str:
    proc = run(["codesign", "--verify", "--deep", "--strict", str(app)])
    if proc.returncode == 0:
        return "valid"
    text = (proc.stderr or proc.stdout).strip()
    if "sealed resource" in text:
        return "broken"
    if "code object is not signed" in text:
        return "unsigned"
    return "invalid"


# --------------------------------------------------------------------------------------
# Патчинг
# --------------------------------------------------------------------------------------


def plan_go(data: bytes, revert_proxy: bool = True, cli_screens: bool = False) -> list[Edit]:
    """План правок для Go-бинарника (hasValidAuth + экраны CLI + откат прокси)."""
    edits: list[Edit] = []
    try:
        gate, state, offset = find_gate(data)
        if state == "unpatched":
            edits.append(Edit(
                offset,
                data[offset:offset + len(gate.fix)],
                gate.fix,
                f"gate: {gate.label}",
            ))
    except LookupError:
        pass

    if cli_screens:
        for gate in CLI_GATES:
            try:
                state, offsets = gate.resolve(data)
                if state == "unpatched":
                    for off in offsets:
                        edits.append(Edit(
                            off,
                            data[off:off + len(gate.fix)],
                            gate.fix,
                            f"gate: {gate.label}",
                        ))
            except LookupError:
                pass

    if revert_proxy:
        for match in re.finditer(re.escape(PROXY_INJECTED), data):
            offset = match.start()
            edits.append(Edit(
                offset,
                data[offset:offset + len(PROXY_ORIGINAL)],
                PROXY_ORIGINAL,
                "revert: AG_LS_PROXY -> https_proxy",
            ))
    return edits


def prepare_ide_js(path: Path) -> bool:
    """Если main.js переписан чужой правкой — возвращает оригинал из бэкапа."""
    try:
        data = read_bytes(path)
    except OSError:
        return False
    if not any(marker in data for marker in IDE_CUSTOM_MARKERS):
        return True
    for suffix in (".ag_backup", ".agybak", ".bak", ".orig"):
        candidate = path.with_name(path.name + suffix)
        if not candidate.is_file():
            continue
        try:
            pristine = read_bytes(candidate)
        except OSError:
            continue
        if not IDE_AUTH_RE.search(pristine):
            continue
        clear_flags(path)
        write_in_place(path, pristine)
        return True
    return False


def plan_ide_js(data: bytes, revert_proxy: bool = True) -> list[Edit]:
    """План правок для main.js IDE."""
    edits: list[Edit] = []

    # Уже пропатчено нами — второй раз ничего не трогаем. Иначе можно случайно
    # наложить старый патч (ветка isGoogleInternal) поверх гейта тира —
    # на IDE 2.6.x он вешает приложение на старте.
    if any(marker in data for marker in IDE_PATCH_MARKERS):
        return edits

    # IDE 2.6.x: гейт допустимости + тир онбординга.
    hit = IDE_TIER_GATE_RE.search(data)
    if hit:
        after = IDE_TIER_GATE_RE.sub(IDE_TIER_GATE_FIX, hit.group(0))
        edits.append(Edit(hit.start(), hit.group(0), after,
                          "gate: tier eligibility -> true"))
    hit = IDE_TIER_ONBOARD_RE.search(data)
    if hit:
        edits.append(Edit(hit.start(), hit.group(0), IDE_TIER_ONBOARD_FIX,
                          "tier: onboardUser free-tier -> standard-tier"))
    if edits:
        return edits

    # IDE 2.5.x: принудительная «внутренняя» ветка входа.
    hit = IDE_AUTH_RE.search(data)
    if hit:
        edits.append(Edit(hit.start(), hit.group(0), IDE_AUTH_FIX,
                          "gate: resetIsTierGCPTos() -> true"))
    return edits


def bundle_writable(app: Path) -> bool:
    """Проверка: можно ли писать внутрь бандла (иначе — App Management)."""
    probe = app / "Contents" / f".agprobe_{os.getpid()}"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


class PatchError(RuntimeError):
    """Патч применён, но результат не прошёл проверку — откатываем."""


def check_js_syntax(path: Path) -> tuple[bool, str]:
    """Проверка синтаксиса JS через node --check (если node установлен)."""
    node = shutil.which("node")
    if not node:
        return True, "node не найден — проверка пропущена"
    proc = run([node, "--check", str(path)])
    if proc.returncode == 0:
        return True, "синтаксис ок"
    lines = (proc.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else "синтаксическая ошибка"
    return False, detail


@dataclass
class Job:
    """Правка одного файла: путь внутри бандла (rel) либо сам файл (rel=None)."""
    label: str
    rel: str | None
    plan: object                      # (bytes, ...) -> list[Edit]
    prepare: object | None = None    # (Path) -> bool, опционально
    verify: object | None = None     # (Path) -> (bool, str), опционально
    revert_proxy: bool = False
    cli_screens: bool = False
    extra: dict = field(default_factory=dict)

    def target_in(self, root: Path) -> Path:
        return root / self.rel if self.rel else root


def apply_job(job: Job, root: Path) -> list[Edit]:
    """Применяет план правок к файлу внутри root. Возвращает применённые правки."""
    path = job.target_in(root)
    if job.prepare is not None:
        job.prepare(path)
    original = read_bytes(path)
    data = bytearray(original)
    if job.plan is plan_go:
        edits = job.plan(bytes(data), job.revert_proxy, job.cli_screens)
    else:
        edits = job.plan(bytes(data), job.revert_proxy)
    if not edits:
        return []
    # Правки с разной длиной сдвигают смещения — применяем с конца к началу.
    # Вырезаем ровно len(before) байт, иначе съедаем соседний код.
    for edit in sorted(edits, key=lambda e: e.offset, reverse=True):
        data[edit.offset:edit.offset + len(edit.before)] = edit.after
    clear_flags(path)
    write_in_place(path, bytes(data))
    if job.verify is not None:
        passed, message = job.verify(path)
        if not passed:
            write_in_place(path, original)
            raise PatchError(f"{job.label}: {message} — правка отменена")
    return edits


def stage_bundle(app: Path) -> Path:
    """Копия бандла во временной папке на том же томе — для обхода App Management."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="ag_stage_", dir="/private/tmp"))
    staged = tmp_dir / app.name
    shutil.copytree(app, staged, symlinks=True)
    return staged


def swap_bundle(original: Path, staged: Path) -> Path:
    """Подменяет бандл целиком. Возвращает путь к бэкапу оригинала."""
    backup = original.with_name(
        f"{original.name}.agbak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    os.rename(original, backup)
    try:
        os.rename(staged, original)
    except OSError:
        os.rename(backup, original)
        raise
    return backup


def restore_file(path: Path, name: str) -> bool:
    journal_path = journal_path_for(path)
    if not journal_path.exists():
        migrated = migrate_inline_journal(path)
        if migrated is None:
            warn(f"{name}: журнала правок нет — откатывать нечего")
            return False
        journal_path = migrated
    journal = Journal.load(journal_path, name)

    # Пересборка из целого бэкапа — откатываем целиком.
    if journal.rebuilt_from:
        source = Path(journal.rebuilt_from)
        if source.is_file():
            try:
                clear_flags(path)
                write_in_place(path, read_bytes(source))
            except OSError as exc:
                err(f"{path}: запись не удалась — {exc}")
                return False
            ok(f"{name}: восстановлено из {source.name}")
            journal.rebuilt_from = ""
            journal.save()
            return True

    if not journal.edits:
        warn(f"{name}: журнал пуст")
        return False

    data = bytearray(read_bytes(path))
    changed = False
    # Смещения правок могут «поехать» из-за разной длины — откатываем с конца.
    for edit in sorted(journal.edits, key=lambda e: e.offset, reverse=True):
        current = bytes(data[edit.offset:edit.offset + len(edit.after)])
        if current == edit.before:
            info(f"{name}: {edit.label} — уже исходное состояние")
            continue
        if current != edit.after:
            warn(f"{name}: {edit.label} — файл изменён сторонними правками, пропускаю")
            continue
        data[edit.offset:edit.offset + len(edit.after)] = edit.before
        changed = True
        ok(f"{name}: откачено — {edit.label}")

    if changed:
        try:
            clear_flags(path)
            write_in_place(path, bytes(data))
        except OSError as exc:
            err(f"{path}: запись не удалась — {exc}")
            return False
    journal.save()
    return True


# --------------------------------------------------------------------------------------
# Процессы и внешний сервис
# --------------------------------------------------------------------------------------

PROCESS_MARKERS = ("Antigravity.app", "Antigravity IDE.app", "language_server", "/agy")


def running_apps() -> list[str]:
    proc = run(["ps", "-Ao", "command"])
    found: list[str] = []
    for line in proc.stdout.splitlines():
        for marker in PROCESS_MARKERS:
            if marker in line and "ps -Ao" not in line:
                if "Antigravity Helper" in line and marker == "Antigravity.app":
                    continue
                short = line.strip()[:90]
                if short not in found:
                    found.append(short)
                break
    return found


CONFEDEN_LABEL = "com.agunlocker.watchdog"


def confeden_service_state() -> str:
    proc = run(["launchctl", "list"])
    if CONFEDEN_LABEL in proc.stdout:
        return "active"
    return "absent"


def stop_confeden() -> bool:
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{CONFEDEN_LABEL}"])
    run(["launchctl", "unload", f"~/Library/LaunchAgents/{CONFEDEN_LABEL}.plist"])
    settings = Path.home() / ".local" / "share" / "agunlocker" / "settings.json"
    if settings.is_file():
        try:
            raw = json.loads(settings.read_text("utf-8"))
            raw["client_patch"] = False
            raw["auto_patch"] = False
            settings.write_text(json.dumps(raw, indent=2, ensure_ascii=False), "utf-8")
            info("отключены client_patch/auto_patch в settings.json анлокера")
        except (OSError, ValueError):
            pass
    return confeden_service_state() == "absent"


# --------------------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------------------


def cmd_status(args) -> int:
    targets = discover()
    if not targets:
        err("Ничего не найдено: нет ни Antigravity.app, ни Antigravity IDE.app, ни agy")
        return 1

    for key, target in targets.items():
        head(f"{target.title}  [{key}]")
        if target.app:
            print(f"   путь:    {target.app}")
            print(f"   версия:  {bundle_version(target.app)}")
            state = signature_state(target.app)
            color = {"valid": "green", "broken": "red"}.get(state, "yellow")
            print(f"   подпись: {c(state, color)}")
            if has_quarantine(target.app):
                print(f"   карантин: {c('есть', 'yellow')}")
        else:
            print(f"   путь:    {target.go[0].path}")

        for go in target.go:
            try:
                data = read_bytes(go.path)
            except OSError as exc:
                err(f"{go.name}: {exc}")
                continue
            try:
                gate, state, offset = find_gate(data)
                color = {"patched": "green", "unpatched": "yellow"}.get(state, "red")
                print(f"   гейт auth: {c(state, color)} ({gate.label} @ 0x{offset:x})")
            except LookupError:
                print(f"   гейт auth: {c('не найден', 'red')} — сигнатура не совпала")

            if target.key in ("cli", "hub"):
                cli_states = []
                for cg in CLI_GATES:
                    try:
                        st, offsets = cg.resolve(data)
                        cli_states.append((cg.label, st, offsets))
                    except LookupError:
                        pass
                if cli_states:
                    for lbl, st, offs in cli_states:
                        col = "green" if st == "patched" else "yellow"
                        offs_str = ", ".join(f"0x{o:x}" for o in offs)
                        print(f"   гейт CLI:  {c(st, col)} ({lbl} @ {offs_str})")
                else:
                    print(f"   гейт CLI:  {c('не найден', 'red')}")

            proxy_hits = len(re.findall(re.escape(PROXY_INJECTED), data))
            if proxy_hits:
                print(f"   прокси:    {c('подменён (AG_LS_PROXY)', 'yellow')} × {proxy_hits}")
            else:
                print(f"   прокси:    штатный (https_proxy)")

        for js in target.js:
            try:
                data = read_bytes(js.path)
            except OSError as exc:
                err(f"{js.name}: {exc}")
                continue
            custom = any(m in data for m in IDE_CUSTOM_MARKERS)
            if any(m in data for m in IDE_PATCH_MARKERS):
                state, color = "снят", "green"
            elif IDE_TIER_GATE_RE.search(data) or IDE_AUTH_RE.search(data):
                state, color = "не снят", "yellow"
            elif custom:
                state, color = "снят (сторонний патч)", "green"
            else:
                state, color = "неизвестно", "red"
            print(f"   гейт JS: {c(state, color)}")

        journal = path_of_journal(target)
        if journal and journal.exists():
            print(f"   журнал:  {journal.name}")
    return 0


def path_of_journal(target: Target) -> Path | None:
    if target.go:
        return journal_path_for(target.go[0].path)
    if target.js:
        return journal_path_for(target.js[0].path)
    return None


def journal_path_for(real: Path) -> Path:
    """Путь журнала для файла. Журналы лежат вне бандлов, чтобы не рвать подпись."""
    key = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:16]
    return JOURNAL_ROOT / f"{real.name}.{key}.json"


def migrate_inline_journal(real: Path) -> Path | None:
    """Переносит старый журнал из бандла во внешнее хранилище."""
    inline = real.with_name(real.name + JOURNAL_SUFFIX)
    if not inline.is_file():
        return None
    target = journal_path_for(real)
    try:
        raw = json.loads(inline.read_text("utf-8"))
    except (OSError, ValueError):
        raw = None
    if raw is not None:
        raw.setdefault("target", real.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(raw, indent=2, ensure_ascii=False), "utf-8")
    try:
        inline.unlink()
    except OSError:
        pass
    return target


def record_journal(real: Path, job: Job, edits: list[Edit]) -> None:
    """Пишет журнал правок во внешнее хранилище (вне .app)."""
    journal_path = journal_path_for(real)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = Journal.load(journal_path, job.label)
    journal.path = journal_path
    for edit in edits:
        if any(e.offset == edit.offset and e.after == edit.after for e in journal.edits):
            continue
        journal.add(edit.offset, edit.before, edit.after, edit.label)
    journal.sha256_after = sha256_file(real)
    journal.save()


def patch_bundle_app(target: Target, args) -> bool:
    """Патчит бандл: на месте, а если macOS запрещает запись — через копию с подменой."""
    app = target.app
    assert app is not None

    jobs: list[Job] = []
    for go in target.go:
        jobs.append(Job(
            go.name,
            str(go.path.relative_to(app)),
            plan_go,
            revert_proxy=not args.keep_proxy,
        ))
    for js in target.js:
        jobs.append(Job(js.name, str(js.path.relative_to(app)), plan_ide_js,
                        prepare=prepare_ide_js, verify=check_js_syntax))

    stage_dir: Path | None = None
    staged: Path | None = None
    if bundle_writable(app):
        root = app
        info("бандл доступен для записи — правлю на месте")
    else:
        stage_dir = Path(tempfile.mkdtemp(prefix="ag_stage_", dir="/private/tmp"))
        staged = stage_dir / app.name
        shutil.copytree(app, staged, symlinks=True)
        root = staged
        info("macOS запрещает запись внутрь бандла (App Management) — "
             "собираю исправленную копию и подменю приложение целиком")

    applied: list[tuple[Job, list[Edit]]] = []
    try:
        for job in jobs:
            try:
                edits = apply_job(job, root)
            except PatchError as exc:
                err(str(exc))
                continue
            if not edits:
                info(f"{job.label}: правки не требуются")
                continue
            for edit in edits:
                ok(f"{job.label}: {edit.label} @ 0x{edit.offset:x}")
            applied.append((job, edits))

        if applied and not args.no_resign:
            success, message = resign_bundle(root)
            if success:
                ok(f"{app.name}: {message}, подпись валидна")
            else:
                err(f"{app.name}: {message}")

        if staged is not None and applied:
            backup = swap_bundle(app, staged)
            staged = None
            ok(f"{app.name}: приложение подменено, оригинал сохранён как {backup.name}")
            for job, edits in applied:
                record_journal(job.target_in(app), job, edits)
        elif applied:
            for job, edits in applied:
                record_journal(job.target_in(app), job, edits)
    finally:
        if stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
    return bool(applied)


def cmd_patch(args) -> int:
    targets = discover()
    wanted = [args.target] if args.target != "all" else list(targets)
    for key in wanted:
        if key not in targets:
            err(f"неизвестная цель: {key}")
            return 1

    running = running_apps()
    if running and not args.force:
        warn("Приложения запущены — правки применятся, но процессы надо перезапустить:")
        for line in running[:5]:
            print(f"    {line}")
        print()

    if not args.keep_proxy:
        state = confeden_service_state()
        if state == "active":
            warn("Активен сторонний анлокер (watchdog) — он вернёт свои правки поверх наших.")
            if args.stop_confeden:
                stop_confeden()
                ok("watchdog анлокера остановлен")
            else:
                info("Остановить его: python3 ag_patcher.py stop-confeden")

    for key in wanted:
        target = targets[key]
        head(f"{target.title}")
        if target.app:
            patch_bundle_app(target, args)
        else:
            # Одиночный файл вне бандла (CLI agy / бэкенд IDE agy).
            is_cli_target = target.key in ("cli", "hub")
            for go in target.go:
                job = Job(go.name, None, plan_go,
                          revert_proxy=not args.keep_proxy,
                          cli_screens=is_cli_target)
                try:
                    edits = apply_job(job, go.path)
                except PatchError as exc:
                    err(str(exc))
                    continue
                if not edits:
                    info(f"{go.name}: правки не требуются")
                    continue
                for edit in edits:
                    ok(f"{go.name}: {edit.label} @ 0x{edit.offset:x}")
                record_journal(go.path, job, edits)
                if not args.no_resign:
                    success, message = resign_macho(go.path)
                    if success:
                        ok(f"{go.name}: {message}")
                    else:
                        err(f"{go.name}: {message}")

    print()
    info("Готово. Перезапустите приложения.")
    return 0


def cmd_restore(args) -> int:
    targets = discover()
    wanted = [args.target] if args.target != "all" else list(targets)
    changed_apps: set[Path] = set()

    for key in wanted:
        target = targets.get(key)
        if not target:
            err(f"неизвестная цель: {key}")
            return 1
        head(f"{target.title}")
        for go in target.go:
            if restore_file(go.path, go.name) and go.app:
                changed_apps.add(go.app)
        for js in target.js:
            if restore_file(js.path, js.name) and js.app:
                changed_apps.add(js.app)

    if not args.no_resign and changed_apps:
        head("Переподпись")
        for app in sorted(changed_apps):
            success, message = resign_bundle(app)
            if success:
                ok(f"{app.name}: {message}")
            else:
                err(f"{app.name}: {message}")
    return 0


def cmd_resign(args) -> int:
    targets = discover()
    wanted = [args.target] if args.target != "all" else list(targets)
    for key in wanted:
        target = targets.get(key)
        if not target or not target.app:
            continue
        head(f"{target.title}")
        success, message = resign_bundle(target.app)
        if success:
            ok(f"{target.app.name}: {message}")
        else:
            err(f"{target.app.name}: {message}")
    return 0


def cmd_doctor(args) -> int:
    head("Состояние системы")

    state = confeden_service_state()
    print(f"   сторонний анлокер: {c(state, 'yellow' if state == 'active' else 'green')}")
    if state == "active":
        info("пока он активен, его watchdog будет заново патчить бинарники")

    running = running_apps()
    print(f"   процессы Antigravity: {len(running)}")
    for line in running[:6]:
        print(f"      {line}")

    head("Сеть до сервисов Google")
    for host in (
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "aicode.googleapis.com",
    ):
        proc = run([
            "curl", "-s", "-o", "/dev/null", "-m", "12",
            "-w", "http=%{http_code} ip=%{remote_ip} t=%{time_total}s",
            f"https://{host}/",
        ])
        if proc.returncode == 0 and proc.stdout:
            print(f"   {host}: {proc.stdout}")
        else:
            print(f"   {host}: {c('недоступен', 'red')} — {proc.stderr.strip()[:60]}")

    head("Подписи")
    for key, target in discover().items():
        if not target.app:
            continue
        state = signature_state(target.app)
        color = {"valid": "green", "broken": "red"}.get(state, "yellow")
        print(f"   {target.title}: {c(state, color)}")
        if state != "valid":
            info("исправить: python3 ag_patcher.py resign --target " + key)
    return 0


def cmd_stop_confeden(args) -> int:
    head("Сторонний анлокер")
    if confeden_service_state() == "absent":
        info("сервис не активен")
    else:
        stop_confeden()
        if confeden_service_state() == "absent":
            ok("watchdog остановлен")
        else:
            err("не удалось остановить — проверьте launchctl list")
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ag_patcher",
        description="Патчер Antigravity (app + IDE) для macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ag_patcher {VERSION}")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="показать состояние целей")
    p_status.set_defaults(func=cmd_status)

    p_patch = sub.add_parser("patch", help="применить патчи")
    p_patch.add_argument("--target", default="all",
                          choices=["all", "manager", "ide", "cli", "hub"])
    p_patch.add_argument("--keep-proxy", action="store_true",
                         help="не откатывать подмену AG_LS_PROXY")
    p_patch.add_argument("--stop-confeden", action="store_true",
                         help="остановить watchdog стороннего анлокера")
    p_patch.add_argument("--no-resign", action="store_true",
                         help="не переподписывать .app")
    p_patch.add_argument("--rebuild-ide", action="store_true",
                         help="пересобрать main.js IDE из чистого бэкапа и пропатчить заново")
    p_patch.add_argument("--force", action="store_true",
                         help="не предупреждать о запущенных приложениях")
    p_patch.set_defaults(func=cmd_patch)

    p_restore = sub.add_parser("restore", help="откатить правки по журналу")
    p_restore.add_argument("--target", default="all",
                            choices=["all", "manager", "ide", "cli", "hub"])
    p_restore.add_argument("--no-resign", action="store_true")
    p_restore.set_defaults(func=cmd_restore)

    p_resign = sub.add_parser("resign", help="переподписать .app")
    p_resign.add_argument("--target", default="all",
                          choices=["all", "manager", "ide", "cli", "hub"])
    p_resign.set_defaults(func=cmd_resign)

    p_doctor = sub.add_parser("doctor", help="диагностика окружения")
    p_doctor.set_defaults(func=cmd_doctor)

    p_stop = sub.add_parser("stop-confeden", help="остановить сторонний анлокер")
    p_stop.set_defaults(func=cmd_stop_confeden)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
