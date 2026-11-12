"""Общий движок патча бинарников Antigravity.

Используется и менеджером (language_server), и CLI agy, и бэкендом
Antigravity IDE — сигнатуры живут в patcher/gates.py, логика применения
здесь. Так две реализации не могут разойтись.

Правила:
  * правки на уровне байт-сигнатур, а не смещений;
  * длина фикса равна длине оригинала — запись in-place, файл не растёт;
  * неоднозначная сигнатура — отказ от правки;
  * идемпотентность: уже применённый патч не накладывается повторно;
  * бэкапы файлов внутри .app хранятся ВНЕ бандла — любой новый файл
    внутри бандла ломает codesign-печать;
  * на macOS запись внутрь .app обходится через staging + подмену бандла.
"""

import os
import shutil
import time

from patcher.gates import plan_go_patches, apply_edits
from patcher.utils.console import info, ok, warn, err
from patcher.utils.file import (
    file_hash,
    file_size,
    format_bytes,
    fix_posix_permissions,
    resign_macos_bundle,
    resign_macos_binary,
    ensure_macos_writable,
    app_bundle_of,
    bundle_writable,
    stage_bundle,
    swap_bundle,
    cleanup_stage,
    write_file_atomic,
    backup_store_dir,
)


def _is_darwin():
    import sys
    return sys.platform == "darwin"


def _preserve_ownership(src, dst):
    """Переносит владельца и права с оригинала на скопированный файл/каталог.

    copytree сохраняет режим и время, но не владельца. Если патчер
    запущен под sudo, скопированный бандл станет root-owned — обновления
    приложения от пользователя потом ломаются. Поэтому восстанавливаем
    uid/gid явно.
    """
    try:
        st = os.lstat(src)
    except OSError:
        return
    try:
        os.lchown(dst, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass
    try:
        os.chmod(dst, st.st_mode & 0o7777)
    except OSError:
        pass
    if os.path.isdir(dst) and not os.path.islink(dst):
        try:
            for name in os.listdir(dst):
                _preserve_ownership(os.path.join(src, name), os.path.join(dst, name))
        except OSError:
            pass


def read_binary(path):
    with open(path, "rb") as f:
        return f.read()


def _backup_key(path):
    import hashlib
    return hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]


def backup_path_for(path, suffix=".agybak"):
    """Путь бэкапа для файла.

    Файл внутри .app-бандла копируется во внешнее хранилище: новый файл
    внутри бандла после подписи ломает codesign-печать, и macOS считает
    приложение повреждённым. Файл вне бандла — копия рядом, как раньше.
    """
    if app_bundle_of(path):
        store = backup_store_dir()
        if store:
            return os.path.join(store, f"{_backup_key(path)}-{os.path.basename(path)}{suffix}")
    return path + suffix


def _make_backup(path, backup_suffix):
    """Снимок исходного файла. Возвращает путь к бэкапу.

    Старый бэкап от другой сборки не перетирается: рядом появляется
    новый с меткой времени. Так откат всегда ведёт к чистому оригиналу.
    """
    bak = backup_path_for(path, backup_suffix)
    if os.path.exists(bak):
        try:
            with open(bak, "rb") as a, open(path, "rb") as b:
                if a.read(4096) == b.read(4096) and file_size(bak) == file_size(path):
                    return bak
        except OSError:
            pass
        stamped = f"{bak}-{time.strftime('%Y%m%d-%H%M%S')}"
        info(f"Backup is stale (app updated) — keeping it and saving a new one: "
             f"{os.path.basename(stamped)}")
        bak = stamped

    ensure_macos_writable(path)
    shutil.copy2(path, bak)
    fix_posix_permissions(bak)
    ok(f"Backup: {os.path.basename(bak)} ({format_bytes(file_size(bak))})")
    return bak


def find_backup(path, backup_suffix=".agybak"):
    """Ищет бэкап: сначала основной, затем самый свежий с меткой времени."""
    candidates = [backup_path_for(path, backup_suffix), path + backup_suffix]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    stamped = []
    for c in candidates:
        if not c:
            continue
        d = os.path.dirname(c)
        base = os.path.basename(c)
        try:
            for name in os.listdir(d):
                if name.startswith(base + "-"):
                    stamped.append(os.path.join(d, name))
        except OSError:
            continue
    if stamped:
        stamped.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return stamped[0]
    return ""


def patch_binary(path, cli_screens=False, revert_proxy=True,
                 backup_suffix=".agybak", label="binary"):
    """Патчит бинарник на месте (или через staging, если бандл защищён).

    Возвращает dict:
        ok        — bool
        edits     — список PatchEdit
        states    — список (label, state)
        method    — 'direct' | 'staged' | ''
        backup    — путь к бэкапу файла
        app_backup— путь к сохранённому оригиналу бандла (при staged)
        message   — текст ошибки
    """
    result = {
        "ok": False, "edits": [], "states": [], "method": "",
        "backup": "", "app_backup": "", "message": "",
    }

    try:
        data = read_binary(path)
    except OSError as e:
        result["message"] = f"Read error: {e}"
        return result

    try:
        edits, states = plan_go_patches(
            data, cli_screens=cli_screens, revert_proxy=revert_proxy)
    except LookupError as e:
        result["message"] = str(e)
        result["states"] = states
        return result

    result["edits"] = edits
    result["states"] = states

    if not edits:
        result["ok"] = True
        result["message"] = "already patched"
        return result

    try:
        new_data = apply_edits(data, edits)
    except ValueError as e:
        result["message"] = str(e)
        return result

    app_path = app_bundle_of(path)

    # --- Прямая запись (файл вне бандла или бандл доступен) ---
    if not app_path or bundle_writable(app_path):
        try:
            backup = _make_backup(path, backup_suffix)
            ensure_macos_writable(path)
            write_file_atomic(path, new_data)
            fix_posix_permissions(path)
            result["method"] = "direct"
            result["backup"] = backup
            if _is_darwin():
                resign_macos_bundle(path)
                resign_macos_binary(path)
            result["ok"] = True
            return result
        except (PermissionError, OSError) as e:
            result["message"] = f"Write error: {e}"
            return result

    # --- Staging: macOS App Management ---
    # Сначала копия бандла, потом правка в копии, переподпись и подмена.
    staged, stage_dir = stage_bundle(app_path)
    try:
        rel = os.path.relpath(os.path.abspath(path), app_path)
        staged_file = os.path.join(staged, rel)
        backup = _make_backup(path, backup_suffix)
        write_file_atomic(staged_file, new_data)
        try:
            _preserve_ownership(os.path.join(app_path, rel), staged_file)
        except OSError:
            pass
        if _is_darwin():
            resign_macos_bundle(staged_file)
        app_backup = swap_bundle(app_path, staged)
        staged = ""
        result["method"] = "staged"
        result["backup"] = backup
        result["app_backup"] = app_backup
        result["ok"] = True
        return result
    except (PermissionError, OSError) as e:
        result["message"] = f"Write error: {e}"
        return result
    finally:
        cleanup_stage(stage_dir)


def restore_binary(path, backup_suffix=".agybak"):
    """Восстанавливает бинарник из бэкапа. Возвращает (ok, message, method)."""
    bak = find_backup(path, backup_suffix)
    if not bak:
        return False, f"No backup for {os.path.basename(path)} (nothing to restore).", ""

    app_path = app_bundle_of(path)

    if not app_path or bundle_writable(app_path):
        try:
            with open(bak, "rb") as f:
                data = f.read()
            ensure_macos_writable(path)
            write_file_atomic(path, data)
            fix_posix_permissions(path)
            if _is_darwin():
                resign_macos_bundle(path)
                resign_macos_binary(path)
            return True, "", "direct"
        except (PermissionError, OSError) as e:
            return False, f"Restore error: {e}", ""

    staged, stage_dir = stage_bundle(app_path)
    try:
        rel = os.path.relpath(os.path.abspath(path), app_path)
        staged_file = os.path.join(staged, rel)
        with open(bak, "rb") as f:
            data = f.read()
        write_file_atomic(staged_file, data)
        if _is_darwin():
            resign_macos_bundle(staged_file)
        app_backup = swap_bundle(app_path, staged)
        staged = ""
        return True, f"app rebuilt, original kept as {os.path.basename(app_backup)}", "staged"
    except (PermissionError, OSError) as e:
        return False, f"Restore error: {e}", ""
    finally:
        cleanup_stage(stage_dir)
