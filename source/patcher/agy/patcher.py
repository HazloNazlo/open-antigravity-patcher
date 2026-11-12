"""Патч Antigravity CLI (agy) и бэкенда Antigravity IDE (agy --hub).

Это один и тот же Go-бинарник в разных местах:

  * ~/.local/bin/agy              — CLI, ставится пользователем;
  * ~/.gemini/bin/agy             — бэкенд, который Antigravity IDE
                                    запускает как `agy --hub`.

Что патчится (все гейты, а не только экран):

  1. Экран допустимости CLI («Eligibility check failed:»). Сигнатуры
     различаются по архитектуре и по версии сборки — см. patcher/gates.py.
  2. Гейт валидатора hasValidAuth — тот же, что в менеджере.
  3. Откат подмены прокси AG_LS_PROXY -> https_proxy: сторонние
     «разблокировщики» гоняют трафик через свой локальный прокси, а его
     сервис часто выключен — приложение тогда показывает «There was a
     network issue…».

Движок общий с менеджером (patcher/utils/binary.py), сигнатуры — в
patcher/gates.py.
"""

import os
import sys

from patcher.constants import COLOR_CYAN
from patcher.utils.console import (
    color,
    info,
    hint,
    ok,
    warn,
    err,
    step,
    print_panel,
)
from patcher.utils.file import (
    file_hash,
    file_size,
    format_bytes,
    fix_posix_permissions,
    resign_macos_bundle,
    resign_macos_binary,
    ensure_macos_writable,
)
from patcher.utils.update import handle_patch_failure
from patcher.utils.admin import terminate_processes
from patcher.utils.binary import patch_binary, restore_binary, find_backup
from patcher.gates import (
    validator_gate_state,
    proxy_revert_count,
    detect_binary_arch,
    CLI_GATES,
    CLI_GATE_GROUP,
)

BAK_EXT = ".agybak"
_agy_status_cache = {}

# Совместимость со старым API (используется test-agy/test_cli_releases.py):
# список (gate_obj, label), где gate_obj.resolve_all(data) -> [(kind, offsets, gate)].
ALL_GATES = [
    (CLI_GATE_GROUP, "eligibility screen off"),
]


def is_locked(path):
    """True, если файл занят (приложение запущено).

    На POSIX замена файла через os.replace возможна даже при работающем
    процессе, поэтому блокирующим считаем только Windows.
    """
    if os.name != "nt":
        return False

    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def get_status(path):
    """('patched'|'unpatched'|'unknown', None) — статус экранов допустимости CLI.

    Совместимая семантика (используется test-agy): считаются только гейты
    экрана CLI и откат прокси. Полный статус (с валидатором) — в
    get_status_detail().
    """
    if not path or not os.path.isfile(path):
        return ("unknown", None)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ("unknown", None)

    target_arch = detect_binary_arch(data)
    states = []
    for gate in CLI_GATES:
        if target_arch not in ("unknown", "universal") and gate.arch != "all" and gate.arch != target_arch:
            continue
        try:
            state, _offsets = gate.resolve(data)
        except LookupError:
            continue
        states.append(state)

    if not states:
        return ("unknown", None)
    if proxy_revert_count(data):
        return ("unpatched", None)
    if all(s == "patched" for s in states):
        return ("patched", None)
    return ("unpatched", None)


def get_status_detail(path):
    """Полный статус: экраны CLI + валидатор hasValidAuth + прокси.

    Возвращает (state, states), где states — список (label, state).
    'patched' только если закрыты ВСЕ найденные гейты.
    """
    if not path or not os.path.isfile(path):
        return ("unknown", [])

    cache_key = None
    try:
        st = os.stat(path)
        cache_key = (os.path.abspath(path), st.st_mtime, st.st_size)
        if cache_key in _agy_status_cache:
            return _agy_status_cache[cache_key]
    except OSError:
        pass

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ("unknown", [])

    target_arch = detect_binary_arch(data)
    states = []
    for gate in CLI_GATES:
        if target_arch not in ("unknown", "universal") and gate.arch != "all" and gate.arch != target_arch:
            continue
        try:
            state, _offsets = gate.resolve(data)
        except LookupError:
            continue
        states.append((gate.label, state))

    validator = validator_gate_state(data)
    states.append(("hasValidAuth", validator))

    proxy_hits = proxy_revert_count(data)
    if proxy_hits:
        states.append(("proxy https_proxy", "unpatched"))

    known = [s for _label, s in states if s in ("patched", "unpatched")]
    if not known:
        result = ("unknown", states)
    elif all(s == "patched" for s in known):
        result = ("patched", states)
    else:
        result = ("unpatched", states)

    if cache_key:
        _agy_status_cache[cache_key] = result
    return result


def is_already_patched(path):
    """True, только если закрыты все гейты (экраны CLI + валидатор + прокси)."""
    return get_status_detail(path)[0] == "patched"


def do_patch_agy(path, is_hub=False):
    """Патчит бинарник agy (CLI или бэкенд IDE)."""
    from patcher.cli import confirmed
    from patcher.utils.captcha import confirm_with_captcha

    label = "Antigravity IDE backend (agy --hub)" if is_hub else "Antigravity CLI"

    if not path or not os.path.isfile(path):
        from patcher.cli import offer_download_and_block
        offer_download_and_block(label)
        return

    hash_before = file_hash(path)
    info(f"Target: {color(path, COLOR_CYAN)}")
    hint(f"Size: {color(format_bytes(file_size(path)), COLOR_CYAN)}")
    print()

    if is_locked(path):
        warn("Binary is locked (agy is running).")
        if confirmed("Would you like to automatically close running agy processes and retry?"):
            terminate_processes(["agy"])
            import time
            time.sleep(1.5)
        else:
            err("File is locked — close Antigravity CLI first.")
            return

    result = patch_binary(
        path,
        cli_screens=True,
        revert_proxy=True,
        backup_suffix=BAK_EXT,
        label="agy",
    )

    if not result["ok"]:
        err(result["message"] or "Patch failed.")
        if sys.platform == "darwin":
            hint("macOS blocked writing into the bundle (App Management).")
            hint("Close Antigravity and try again — the patcher rebuilds the app.")
        else:
            hint("No write access — re-run as admin/root and close agy first.")
        handle_patch_failure()
        return

    if not result["edits"]:
        hint("agy already patched (all gates).")
        ok("Nothing to do.")
        return

    _agy_status_cache.clear()
    hash_after = file_hash(path)

    if result["method"] == "staged":
        ok("macOS blocked in-place writing — app was rebuilt and swapped.")
        if result["app_backup"]:
            ok(f"Original app kept as: {os.path.basename(result['app_backup'])}")

    print()
    for gate_label, state in result["states"]:
        step(gate_label, state in ("patched", "unpatched"),
             "already patched" if state == "patched" else
             ("reverted" if "proxy" in gate_label else "patched"))
    print()

    panel_rows = [
        ("Target", os.path.basename(path)),
        ("Gates", f"{len(result['edits'])} applied"),
    ]
    if hash_before and hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("PATCH COMPLETE", panel_rows)
    hint("Restart Antigravity CLI for the change to take effect.")


def do_restore_agy(path, is_hub=False):
    from patcher.cli import confirmed

    if not os.path.isfile(path):
        err(f"Target is not a file: {path}")
        return

    bak = find_backup(path, BAK_EXT)
    if not bak:
        warn(f"No backup for {os.path.basename(path)} (nothing to restore).")
        return

    status, _ = get_status(path)
    if status != "patched":
        warn("agy is not patched — skipping restore (backup may be a different build).")
        if not confirmed("Restore from backup anyway?"):
            hint("Restore cancelled.")
            return

    if is_locked(path):
        err("Binary is locked — close Antigravity CLI first.")
        return

    if not confirmed("Restore agy from backup?"):
        hint("Restore cancelled.")
        return

    hash_before = file_hash(path)

    ok_flag, message, method = restore_binary(path, BAK_EXT)
    if not ok_flag:
        err(message or "Restore failed.")
        return
    _agy_status_cache.clear()
    if method == "staged" and message:
        ok(message)

    hash_after = file_hash(path)
    print()
    panel_rows = [("Target", os.path.basename(path))]
    if hash_before and hash_after and hash_before != hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("RESTORE COMPLETE", panel_rows)
