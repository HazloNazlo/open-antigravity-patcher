"""Патч менеджера Antigravity 2.0 (language_server).

Бэкенд language_server — Go-бинарник. В нём два следствия региональных
ограничений:

  1. Гейт валидатора hasValidAuth: пока байт не равен True, клиент
     уводит выполнение в ветку «аккаунт не допущен».
  2. Подмена переменной прокси https_proxy -> AG_LS_PROXY, которую
     вшивают сторонние «разблокировщики»: их локальный сервис выключен —
     и приложение выдаёт «There was a network issue…».

Сигнатуры живут в patcher/gates.py, логика применения — в
patcher/utils/binary.py: тот же движок использует и CLI agy.
"""

import os
import sys

from packaging.version import Version
from enum import Enum

from patcher.constants import COLOR_CYAN, MIN_ANTIGRAVITY_VERSION
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
)
from patcher.utils.update import handle_patch_failure
from patcher.utils.admin import terminate_processes
from patcher.utils.binary import patch_binary, restore_binary, find_backup
from patcher.gates import validator_gate_state, proxy_revert_count

BAK_EXT = ".agybak"


class VersionStatus(Enum):
    OK = "ok"
    TOO_OLD = "too_old"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"


from patcher.manager.discovery import find_asar_relative_to_manager, read_package_json_from_asar


def check_antigravity_version(asar_path):
    ver_str = read_package_json_from_asar(asar_path)
    if ver_str is None:
        return VersionStatus.NOT_FOUND, None

    try:
        detected = Version(ver_str)
        minimum = Version(MIN_ANTIGRAVITY_VERSION)
        status = VersionStatus.OK if detected >= minimum else VersionStatus.TOO_OLD
        return status, ver_str
    except Exception:
        return VersionStatus.PARSE_ERROR, ver_str


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
    """('patched'|'unpatched'|'unknown', None) — статус гейта менеджера."""
    if not path or not os.path.isfile(path):
        return ("unknown", None)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ("unknown", None)
    return (validator_gate_state(data), None)


def is_already_patched(path):
    return get_status(path)[0] == "patched"


def do_patch_manager(path):
    from patcher.cli import confirmed
    from patcher.utils.captcha import confirm_with_captcha

    if not path or not os.path.isfile(path):
        from patcher.cli import offer_download_and_block
        offer_download_and_block("Antigravity 2.0")
        return

    hash_before = file_hash(path)
    info(f"Target: {color(path, COLOR_CYAN)}")
    hint(f"Size: {color(format_bytes(file_size(path)), COLOR_CYAN)}")

    # Проверка версии Antigravity (считывается из package.json в app.asar)
    asar_path = find_asar_relative_to_manager(path)
    if asar_path:
        ver_status, ver_str = check_antigravity_version(asar_path)
        if ver_status == VersionStatus.TOO_OLD:
            err(f"Unsupported version: {ver_str}")
            err(f"Minimum required: {MIN_ANTIGRAVITY_VERSION}")
            hint("Please update Antigravity and try again.")
            if not confirmed("Proceed anyway?"):
                return
        elif ver_status == VersionStatus.NOT_FOUND:
            warn("Could not detect Antigravity version (package.json not found in ASAR).")
            if not confirmed("Proceed without version check?"):
                return
        elif ver_status == VersionStatus.PARSE_ERROR:
            warn(f"Could not parse version string: {ver_str}")
            if not confirmed("Proceed anyway?"):
                return
    print()

    if is_locked(path):
        warn("Binary is locked (Antigravity Manager is running).")
        if confirmed("Would you like to automatically close running Antigravity processes and retry?"):
            terminate_processes(["Antigravity", "Antigravity IDE", "antigravity",
                                  "antigravity-ide", "language_server"])
            import time
            time.sleep(1.5)
        else:
            err("File is locked — close the app first.")
            return

    result = patch_binary(
        path,
        cli_screens=False,
        revert_proxy=True,
        backup_suffix=BAK_EXT,
        label="language_server",
    )

    if not result["ok"]:
        err(result["message"] or "Patch failed.")
        if sys.platform == "darwin":
            hint("macOS blocked writing inside Antigravity.app (App Management).")
            hint("Close Antigravity and try again — the patcher rebuilds the app.")
        else:
            hint("No write access — re-run as admin/root and close Antigravity first.")
        handle_patch_failure()
        return

    if not result["edits"]:
        hint("Antigravity Manager already patched — no changes needed.")
        ok("Nothing to do.")
        return

    hash_after = file_hash(path)

    if result["method"] == "staged":
        ok("macOS blocked in-place writing — app was rebuilt and swapped.")
        if result["app_backup"]:
            ok(f"Original app kept as: {os.path.basename(result['app_backup'])}")

    print()
    for label, state in result["states"]:
        step(label, state in ("patched", "unpatched"),
             "already patched" if state == "patched" else
             ("reverted" if "proxy" in label else "patched"))
    print()

    panel_rows = [
        ("Target", os.path.basename(path)),
        ("Gates", f"{len(result['edits'])} applied"),
    ]
    if hash_before and hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("PATCH COMPLETE", panel_rows)
    hint("Restart Antigravity/Antigravity IDE for the changes to take effect.")


def do_restore_manager(path):
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
        warn("Manager is not patched — skipping restore (backup may be a different build).")
        if not confirmed("Restore from backup anyway?"):
            hint("Restore cancelled.")
            return

    if is_locked(path):
        err("Binary is locked — close the app first.")
        return

    if not confirmed("Restore Manager from backup?"):
        hint("Restore cancelled.")
        return

    hash_before = file_hash(path)

    ok_msg, message, method = restore_binary(path, BAK_EXT)
    if not ok_msg:
        err(message or "Restore failed.")
        return
    if method == "staged" and message:
        ok(message)

    hash_after = file_hash(path)
    print()
    panel_rows = [("Target", os.path.basename(path))]
    if hash_before and hash_after and hash_before != hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("RESTORE COMPLETE", panel_rows)
