"""Патч Antigravity IDE (main.js).

Что патчится
------------
Antigravity IDE 2.6.x — гейт допустимости переехал в confirmUserForService:

    const s = !!r || !i;                  ; r — есть free-tier в allowedTiers,
                                         ; i — причина отказа в ineligibleTiers
    if (s) { await onboardUser("free-tier", t); ... }

Правка 1: s = true — клиент перестаёт проверять допуск к free-tier.
Правка 2: onboardUser("free-tier") -> onboardUser("standard-tier") — сервер
отклоняет онбординг в free-tier для аккаунтов вне поддерживаемых регионов,
а standard-tier принимает.

Antigravity IDE 2.5.x — проверка была в ветке «внутреннего» входа
(resetIsTierGCPTos(),this.<x>.isGoogleInternal). Поддерживается как
запасной вариант, если тир-гейт в сборке не найден.

Важно: legacy-патч нельзя накладывать поверх тир-гейта 2.6.x — IDE
зависает на старте в модальном окне NSAlert. Поэтому порядок строго
такой: сначала ищем тир-гейт, legacy — только если тир-гейта нет.

Патч идемпотентен: при наличии наших маркеров повторный запуск ничего
не меняет.
"""

import os
import sys
import shutil
import time

from patcher.constants import (
    MIN_AG_VERSION,
)
from patcher.gates import (
    IDE_PATCH_MARKERS,
    IDE_CUSTOM_MARKERS,
    plan_ide_js,
    ide_js_state,
)
from patcher.utils.console import (
    info,
    hint,
    ok,
    warn,
    err,
    step,
    print_panel,
)
from patcher.utils.update import handle_patch_failure
from patcher.utils.admin import terminate_processes
from patcher.utils.file import (
    file_hash,
    file_size,
    format_bytes,
    fix_posix_permissions,
    resign_macos_bundle,
    remove_macos_immutable_flags,
    remove_macos_quarantine,
    find_app_bundle,
    app_bundle_of,
    bundle_writable,
    stage_bundle,
    swap_bundle,
    cleanup_stage,
    patch_file_in_bundle,
    check_js_syntax,
    write_file_atomic,
    backup_store_dir,
)
from patcher.ide.discovery import (
    check_ag_version,
    parse_version_safe,
    VersionStatus,
)


def _ide_cache_dirs():
    """VS Code CachedData / Code Cache dirs to drop after patching main.js, so the IDE
    recompiles the patched bytes instead of replaying a stale compile cache. The user-data
    folder is the product nameLong ('Antigravity IDE') under each OS's app-data root."""
    home = os.path.expanduser("~")
    if os.name == "nt":
        bases = [os.path.expandvars(p) for p in
                 (r"%USERPROFILE%\scoop\persist\antigravity-ide\data\user-data",
                  r"%APPDATA%\Antigravity IDE")]
    elif sys.platform == "darwin":
        bases = [os.path.join(home, "Library", "Application Support", "Antigravity IDE")]
    else:                                                  # Linux (respect XDG_CONFIG_HOME)
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        bases = [os.path.join(cfg, "Antigravity IDE")]
    dirs = []
    for base in bases:
        dirs += [os.path.join(base, "CachedData"),
                 os.path.join(base, "Code Cache", "js")]
    return dirs


def _as_bytes(content):
    """Принимает str или bytes — main.js читается как текст, но патчим байты."""
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8", errors="surrogateescape")


def is_already_patched(content):
    """True, если наши маркеры уже стоят в main.js."""
    data = _as_bytes(content)
    return any(marker in data for marker in IDE_PATCH_MARKERS)


def has_foreign_patch(content):
    """True, если main.js правил сторонний «разблокировщик»."""
    data = _as_bytes(content)
    return any(marker in data for marker in IDE_CUSTOM_MARKERS)


def apply_patches(content, ag_version=None):
    """Применяет патч IDE к main.js.

    Возвращает (new_content, results), где results — список dict'ов
    для step() в интерфейсе.
    """
    data = _as_bytes(content)
    results = []

    try:
        edits, states = plan_ide_js(data)
    except LookupError as e:
        return data, [{
            "Name": "auth gate",
            "Applied": False,
            "Detail": str(e),
        }]

    for label, state in states:
        applied = state == "patched"
        detail = {
            "patched": "already patched",
            "unpatched": "pattern found",
            "not-found": "pattern not found",
            "unknown": "unknown",
        }.get(state, state)
        results.append({
            "Name": label,
            "Applied": applied or state == "unpatched",
            "Detail": detail,
        })

    if not edits:
        # Ничего не найдено — если маркеры на месте, значит уже пропатчено.
        if is_already_patched(data):
            for r in results:
                if r["Name"].endswith("(2.6.x)") or "tier" in r["Name"]:
                    r["Applied"] = True
                    r["Detail"] = "already patched"
        return data, results

    new_data = data
    for before, after, _label in edits:
        # Замены применяются с конца к началу: длины могут отличаться.
        new_data = new_data.replace(before, after, 1)

    for _before, _after, label in edits:
        results.append({
            "Name": label,
            "Applied": True,
            "Detail": "applied",
        })

    return new_data, results


def warn_about_unsafe_backup(main_js_path, installed_version_str=None, current_content=None):
    """Проверяет бэкап перед восстановлением. Возвращает (ok, has_warnings)."""
    backup_path = _backup_path(main_js_path)
    if not backup_path or not os.path.exists(backup_path):
        return True, False

    backup_size = file_size(backup_path)
    current_size = file_size(main_js_path)
    warnings = []

    if backup_size <= 2048:
        warnings.append(
            f"backup size is only {format_bytes(backup_size)} and it looks almost empty"
        )
    elif backup_size < 4096 or (current_size > 0 and backup_size < current_size // 10):
        warnings.append(
            f"backup is much smaller than expected "
            f"({format_bytes(backup_size)} vs {format_bytes(current_size)})"
        )

    if not warnings:
        return True, False

    for warning in warnings:
        warn(f"Backup warning: {warning}")
    warn("Restoring this backup may break Antigravity IDE.")
    warn(f"Backup kept: {os.path.basename(backup_path)}")
    return True, True


def _backup_path(main_js_path):
    """Путь резервной копии main.js.

    Копия хранится ВНЕ бандла: любой новый файл внутри .app после
    подписи ломает codesign-печать, и macOS считает приложение
    повреждённым («улетает в корзину»).
    """
    store = backup_store_dir()
    if not store:
        return main_js_path + ".bak"
    key = hashlib_name(main_js_path)
    return os.path.join(store, f"{key}-main.js.bak")


def hashlib_name(path):
    """Короткий стабильный ключ для имени файла во внешнем хранилище."""
    import hashlib
    return hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]


def _write_backup(main_js_path, original_bytes):
    """Сохраняет оригинал main.js во внешнее хранилище. Возвращает путь."""
    backup_path = _backup_path(main_js_path)
    try:
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        with open(backup_path, "wb") as f:
            f.write(original_bytes)
        ok(f"Backup: {os.path.basename(backup_path)} "
           f"({format_bytes(file_size(backup_path))})")
        return backup_path
    except Exception as e:
        warn(f"Backup error: {e}")
        return ""


def _commit_to_bundle(main_js_path, new_bytes):
    """Записывает новые байты в main.js с учётом App Management.

    Возвращает (ok, method, backup_bundle_path, message).
    """
    app_path = app_bundle_of(main_js_path)

    if not app_path:
        try:
            write_file_atomic(main_js_path, new_bytes)
            fix_posix_permissions(main_js_path)
            return True, "direct", "", ""
        except Exception as e:
            return False, "", "", str(e)

    if bundle_writable(app_path):
        # Бандл доступен для записи — правим на месте.
        try:
            write_file_atomic(main_js_path, new_bytes)
            fix_posix_permissions(main_js_path)
            resign_macos_bundle(main_js_path)
            return True, "direct", "", ""
        except Exception as e:
            return False, "", "", str(e)

    # macOS App Management: собираем исправленную копию и подменяем целиком.
    staged, stage_dir = stage_bundle(app_path)
    try:
        rel = os.path.relpath(os.path.abspath(main_js_path), app_path)
        staged_file = os.path.join(staged, rel)
        write_file_atomic(staged_file, new_bytes)
        fix_posix_permissions(staged_file)
        resign_macos_bundle(staged_file)
        backup = swap_bundle(app_path, staged)
        staged = ""
        return True, "staged", backup, ""
    except Exception as e:
        return False, "", "", str(e)
    finally:
        cleanup_stage(stage_dir)


def do_patch(main_js_path, show_search_line=False):
    from patcher.cli import confirmed
    from patcher.utils.captcha import confirm_with_captcha

    if not main_js_path or not os.path.isfile(main_js_path):
        from patcher.cli import offer_download_and_block
        offer_download_and_block("Antigravity IDE")
        return

    ver_status, ver_str = check_ag_version(main_js_path)
    parsed_version = parse_version_safe(ver_str)

    if ver_status == VersionStatus.TOO_OLD:
        err(f"Unsupported version: {ver_str}")
        err(f"Minimum required: {MIN_AG_VERSION}")
        hint("Please update Antigravity IDE and try again.")
        if not confirmed("Proceed anyway?"):
            return
    elif ver_status == VersionStatus.NOT_FOUND:
        warn("Could not detect Antigravity IDE version (registry key not found).")
        if not confirmed("Proceed without version check?"):
            return
    elif ver_status == VersionStatus.PARSE_ERROR:
        warn(f"Could not parse version string: {ver_str}")
        if not confirmed("Proceed anyway?"):
            return
    # VersionStatus.OK — продолжаем без вопросов

    try:
        with open(main_js_path, "rb") as f:
            original = f.read()
    except Exception as e:
        err(f"Read error: {e}")
        return

    current_is_patched = is_already_patched(original)

    if current_is_patched:
        hint("File appears already patched.")
        if not confirm_with_captcha("Apply main.js patches anyway?"):
            return

    if has_foreign_patch(original):
        warn("main.js contains a third-party unlock patch.")
        hint("Patching on top of it may leave the app unstable.")
        if not confirmed("Continue anyway?"):
            return

    info("Applying patches...")
    print()

    new_data, results = apply_patches(original, ag_version=parsed_version)

    applied = 0
    for r in results:
        if r.get("Applied"):
            applied += 1
        step(r["Name"], r.get("Applied", False), r.get("Detail", ""))
    print()

    if new_data == original:
        if current_is_patched:
            ok("File is already patched — no changes needed.")
            return
        err("No patches applied.")
        handle_patch_failure()
        return

    # --- Проверка синтаксиса ДО записи (node --check) ---
    tmp_check = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"ag_ide_check_{os.getpid()}.js")
    try:
        with open(tmp_check, "wb") as f:
            f.write(new_data)
        syntax_ok, syntax_msg = check_js_syntax(tmp_check)
    finally:
        try:
            os.unlink(tmp_check)
        except OSError:
            pass

    if not syntax_ok:
        err(f"JavaScript syntax check failed: {syntax_msg}")
        err("Patch was NOT written — the file is left untouched.")
        handle_patch_failure()
        return
    if syntax_msg and "ok" in syntax_msg:
        ok(syntax_msg)

    # --- БЭКАП (вне бандла) ---
    backup_path = _write_backup(main_js_path, original)
    if not backup_path:
        err("Could not create a backup — patching aborted.")
        handle_patch_failure()
        return

    hash_before = file_hash(main_js_path)

    write_ok, method, bundle_backup, error = _commit_to_bundle(main_js_path, new_data)
    if not write_ok:
        err(f"Write error: {error}")
        if sys.platform == "darwin":
            hint("macOS blocked writing inside Antigravity IDE.app (App Management).")
            hint("Close Antigravity IDE and re-run the patcher.")
        else:
            hint("Close Antigravity IDE and re-run the patcher.")
        handle_patch_failure()
        return

    if method == "staged":
        ok("macOS blocked in-place writing — app was rebuilt and swapped.")
        if bundle_backup:
            ok(f"Original app kept as: {os.path.basename(bundle_backup)}")
    elif method == "direct":
        resign_macos_bundle(main_js_path)

    # Очистка кэша скомпилированных JS файлов VS Code (CachedData / Code Cache)
    info("Clearing IDE compile caches...")
    for c in _ide_cache_dirs():
        try:
            if os.path.isdir(c):
                shutil.rmtree(c, ignore_errors=True)
        except Exception:
            pass

    hash_after = file_hash(main_js_path)

    # --- Проверка результата: маркеры на месте? ---
    try:
        with open(main_js_path, "rb") as f:
            verify_data = f.read()
    except OSError:
        verify_data = b""
    final_state = ide_js_state(verify_data) if verify_data else "unknown"
    if final_state != "patched":
        err("Post-patch verification failed — gate is not in patched state.")
        warn("Restore from backup (RESTORE menu) to roll back.")
    else:
        ok("Patch verified in file")

    panel_rows = [
        ("Target", os.path.basename(main_js_path)),
        ("Patches", f"{applied}/{len(results)} applied"),
        ("State", final_state),
    ]
    if backup_path:
        panel_rows.append(("Backup", f"{os.path.basename(backup_path)} "
                                     f"({format_bytes(file_size(backup_path))})"))
    if hash_before and hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    panel_rows.append(("Done", time.strftime('%H:%M:%S')))
    print_panel("PATCH COMPLETE", panel_rows)
    hint("Restart Antigravity IDE and sign in.")


def do_restore(main_js_path, show_search_line=False):
    from patcher.cli import confirmed

    current_content = None
    try:
        with open(main_js_path, "rb") as f:
            current_content = f.read()
    except Exception:
        pass

    backup_ok, backup_has_warnings = warn_about_unsafe_backup(
        main_js_path, current_content=current_content
    )
    if not backup_ok:
        return

    backup_path = _backup_path(main_js_path)
    legacy_backup = main_js_path + ".bak"

    if not os.path.exists(backup_path) and os.path.exists(legacy_backup):
        backup_path = legacy_backup

    if not backup_path or not os.path.exists(backup_path):
        err(f"Backup file not found for {os.path.basename(main_js_path)}")
        hint("No backup was created by this patcher for this file.")
        return

    try:
        with open(backup_path, "rb") as f:
            data = f.read()
    except Exception as e:
        err(f"Could not read backup: {e}")
        return

    backup_size = file_size(backup_path)
    if backup_size <= 2048:
        err("Backup looks too small — may be corrupted!")
        if not confirmed("Restore anyway?"):
            hint("Restore cancelled.")
            return

    if is_already_patched(data):
        warn("Backup itself appears to be patched!")
        if not confirmed("Restore this patched backup?"):
            hint("Restore cancelled.")
            return

    restore_question = "Restore this backup anyway?" if backup_has_warnings else "Restore backup?"
    if not confirmed(restore_question):
        hint("Restore cancelled.")
        return

    hash_before = file_hash(main_js_path)

    write_ok, method, bundle_backup, error = _commit_to_bundle(main_js_path, data)
    if not write_ok:
        err(f"Restore error: {error}")
        return

    if method == "staged" and bundle_backup:
        ok(f"Original app kept as: {os.path.basename(bundle_backup)}")

    hash_after = file_hash(main_js_path)

    panel_rows = [("Target", os.path.basename(main_js_path))]
    if hash_before and hash_after and hash_before != hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    panel_rows.append(("Done", time.strftime('%H:%M:%S')))
    print_panel("RESTORE COMPLETE", panel_rows)
    hint("Restart Antigravity IDE.")
