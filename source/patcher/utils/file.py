import os
import sys
import errno
import hashlib
import time
import shutil
import subprocess
import tempfile

try:
    import pwd
except ImportError:
    pwd = None


def file_hash(path):
    """Возвращает SHA-256 файла или None при ошибке."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def format_bytes(size_bytes):
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def fix_posix_permissions(path):
    """Ensures the path and its contents are owned by the invoking user on POSIX when running via sudo."""
    if os.name != "posix" or os.getuid() != 0:
        return

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")

    if sudo_uid and sudo_gid:
        try:
            subprocess.run(["chown", "-R", f"{sudo_uid}:{sudo_gid}", path], check=False, timeout=30)
        except Exception:
            pass


def get_posix_invoking_user_home():
    """Returns the invoking user's home on POSIX, even when running via sudo."""
    if os.name != "posix":
        return ""

    if pwd is not None:
        sudo_uid = os.environ.get("SUDO_UID")
        if sudo_uid:
            try:
                return pwd.getpwuid(int(sudo_uid)).pw_dir
            except Exception:
                pass

        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            try:
                return pwd.getpwnam(sudo_user).pw_dir
            except Exception:
                pass

    home = os.environ.get("HOME")
    if home:
        return home

    return os.path.expanduser("~")


def backup_json_file(path):
    if not os.path.exists(path):
        return ""

    base = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    backup_path = base
    counter = 1
    while os.path.exists(backup_path):
        counter += 1
        backup_path = f"{base}-{counter}"

    shutil.copy2(path, backup_path)
    fix_posix_permissions(backup_path)
    return backup_path


def find_app_bundle(path):
    """Поднимается вверх от path до первой директории, оканчивающейся на .app.

    Используется только на macOS, чтобы определить корень .app-бандла
    для переподписи после модификации main.js.
    """
    p = os.path.abspath(path)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return ""


def remove_macos_immutable_flags(path):
    """Снимает флаги uchg/schg с файла или директории на macOS.

    На macOS файлы внутри .app-бандлов могут иметь флаги immutable,
    которые блокируют запись даже для root (Errno 1 Operation not
    permitted при shutil.copy2 / создании бэкапа внутри бандла).
    Вызывать перед попыткой записи в .app-бандл.

    Возвращает True, если chflags отработал (или платформа не darwin).
    """
    import sys
    if sys.platform != "darwin":
        return True
    ok_flag = False
    # -R работает и для одиночных файлов, и для директорий.
    # Пробуем сначала связку nouchg,noschg, затем по отдельности
    # (noschg без Recovery может не сняться — это нормально).
    # Абсолютный путь /usr/bin/chflags: в frozen-сборках PATH может быть пустым.
    for args in (
        ["/usr/bin/chflags", "-R", "nouchg,noschg", path],
        ["/usr/bin/chflags", "-R", "nouchg", path],
        ["/usr/bin/chflags", "nouchg", path],
    ):
        try:
            res = subprocess.run(
                args,
                check=False, capture_output=True, timeout=30,
            )
            if res.returncode == 0:
                ok_flag = True
                break
        except FileNotFoundError:
            break
        except Exception:
            continue
    return ok_flag


def ensure_macos_writable(path):
    """Готовит путь внутри .app-бандла к записи на macOS.

    Снимает immutable-флаги с самого файла и с корня .app-бандла,
    а также quarantine с бандла. Безопасен на других ОС (no-op).
    Вызывать ПЕРЕД созданием бэкапа и ПЕРЕД атомарной заменой,
    т.к. обе операции пишут внутрь бандла.
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        if os.path.isfile(path) or os.path.isdir(path):
            remove_macos_immutable_flags(path)
    except Exception:
        pass
    try:
        app_path = find_app_bundle(path)
        if app_path and os.path.normcase(app_path) != os.path.normcase(path):
            remove_macos_immutable_flags(app_path)
            remove_macos_quarantine(app_path)
    except Exception:
        pass


def remove_macos_quarantine(path):
    """Снимает атрибут com.apple.quarantine с .app-бандла.

    Используется встроенная утилита /usr/bin/xattr — дополнительные
    Python-пакеты не нужны, работает и во frozen-сборках без os.*xattr API.
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", path],
            check=False, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _macos_entitlements(app_path):
    """Читает entitlements текущей подписи бандла.

    Важно: без сохранения entitlements Electron-приложение падает на старте —
    теряется com.apple.security.cs.allow-jit, без которого V8 не может JIT.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", app_path],
            check=False, capture_output=True, text=True, timeout=30,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return {}
        start = raw.find("<?xml")
        if start < 0:
            start = raw.find("<plist")
        if start < 0:
            return {}
        import plistlib
        return plistlib.loads(raw[start:].encode("utf-8"))
    except Exception:
        return {}


def _macos_signables(app_path):
    """Все объекты кода внутри бандла: вложенные Mach-O и вложенные бандлы."""
    import plistlib  # noqa: F401  (используется ниже в проверке magic)
    magics = {
        b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
    }
    suffixes = (".app", ".framework", ".xpc", ".bundle", ".plugin", ".appex")
    items = []
    for root, dirs, files in os.walk(app_path):
        for name in list(dirs):
            if name.endswith(suffixes):
                items.append(os.path.join(root, name))
        for name in files:
            candidate = os.path.join(root, name)
            if os.path.islink(candidate) or not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "rb") as fh:
                    if fh.read(4) in magics:
                        items.append(candidate)
            except OSError:
                continue
    # Вложенные — раньше, внешний бандл — последним.
    items.sort(key=lambda p: p.count(os.sep), reverse=True)
    return items


def resign_macos_bundle(main_js_path):
    """Переподписывает .app ad-hoc подписью после изменения файлов внутри него.

    Порядок «изнутри наружу»: сначала вложенные Mach-O и вложенные бандлы,
    затем сам .app. Entitlements сохраняются, а Hardened Runtime остаётся
    включённым — иначе Electron не стартует (падает V8 без allow-jit).
    Дополнительно добавляется disable-library-validation: ad-hoc подпись не
    имеет Team ID, и Electron Framework иначе отвергается проверкой библиотек.
    """
    import sys
    if sys.platform != "darwin":
        return

    from patcher.utils.console import info, ok, warn

    app_path = find_app_bundle(main_js_path)
    if not app_path:
        # main.js лежит не внутри .app (например, portable-копия) — пропускаем
        return

    info(f"Re-signing {os.path.basename(app_path)} (ad-hoc, изнутри наружу)...")
    try:
        import plistlib
        import tempfile

        entitlements = _macos_entitlements(app_path)
        entitlements["com.apple.security.cs.disable-library-validation"] = True
        entitlements.setdefault("com.apple.security.cs.allow-jit", True)

        with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as tmp:
            plistlib.dump(entitlements, tmp)
            ent_path = tmp.name

        try:
            remove_macos_immutable_flags(app_path)
            for item in _macos_signables(app_path):
                try:
                    mode = os.stat(item).st_mode
                    if not mode & 0o200:
                        os.chmod(item, mode | 0o200)
                except OSError:
                    pass
                subprocess.run(
                    ["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
                     "--timestamp=none", "--entitlements", ent_path, item],
                    check=False, capture_output=True, text=True, timeout=120,
                )
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
                 "--timestamp=none", "--entitlements", ent_path, app_path],
                check=True, capture_output=True, text=True, timeout=120,
            )
        finally:
            try:
                os.unlink(ent_path)
            except OSError:
                pass

        verify = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", app_path],
            check=False, capture_output=True, text=True, timeout=120,
        )
        if verify.returncode != 0:
            warn(f"codesign verify failed: {(verify.stderr or '').strip()}")
            return
        ok("Ad-hoc signature applied")
    except FileNotFoundError:
        warn("codesign not found - install Xcode Command Line Tools")
        return
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        warn(f"codesign failed: {stderr}")
        return
    except Exception as e:
        warn(f"codesign failed: {e}")
        return

    remove_macos_quarantine(app_path)


def resign_macos_binary(path):
    """Переподписывает Antigravity 2.0 бинарь ad-hoc подписью после его модификации.

    На Apple Silicon macOS немедленно SIGKILL-ит процесс, если code signature
    не совпадает с содержимым. codesign --force --sign - заменяет Developer ID
    подпись на ad-hoc (signing identity «-»), чего достаточно для локального запуска.
    """
    import sys
    if sys.platform != "darwin":
        return

    from patcher.utils.console import info, ok, warn

    real = os.path.realpath(path)
    info(f"Re-signing {os.path.basename(real)} (ad-hoc)...")
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", real],
            check=True, capture_output=True, text=True, timeout=60,
        )
        ok("Ad-hoc signature applied")
    except FileNotFoundError:
        warn("codesign not found — install Xcode Command Line Tools")
    except subprocess.CalledProcessError as e:
        warn(f"codesign failed: {(e.stderr or '').strip()}")


# --------------------------------------------------------------------------- #
#  Обход macOS App Management                                                  #
# --------------------------------------------------------------------------- #
#
# macOS запрещает писать внутрь чужого .app-бандла: даже под root запись
# в /Applications/Antigravity.app даёт EPERM (Errno 1). Переименовать сам
# бандл при этом можно. Поэтому патч собирается в копии на том же томе
# (rename работает только внутри одного тома), а потом подменяется целиком.


def bundle_writable(app_path):
    """True, если внутрь бандла можно писать (иначе — App Management).

    Проба создаётся и сразу удаляется, чтобы состояние бандла не менялось.
    """
    if not app_path or not os.path.isdir(app_path):
        return False
    probe_dir = os.path.join(app_path, "Contents")
    if not os.path.isdir(probe_dir):
        probe_dir = app_path
    probe = os.path.join(probe_dir, f".agprobe_{os.getpid()}_{int(time.time())}")
    try:
        with open(probe, "wb"):
            pass
    except OSError:
        return False
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def stage_bundle(app_path):
    """Копирует .app-бандл во временную папку на том же томе.

    Возвращает (staged_path, stage_dir). stage_dir нужно удалить через
    cleanup_stage() после подмены (в сам staged_path живёт уже новый бандл).
    """
    stage_root = "/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir()
    stage_dir = tempfile.mkdtemp(prefix="ag_stage_", dir=stage_root)
    staged = os.path.join(stage_dir, os.path.basename(app_path))
    shutil.copytree(app_path, staged, symlinks=True)
    return staged, stage_dir


def swap_bundle(original_path, staged_path):
    """Подменяет бандл целиком: оригинал -> <name>.agbak-<дата>, staged -> original.

    Возвращает путь к сохранённому оригиналу. При ошибке переименования
    оригинал возвращается на место.
    """
    backup = original_path + ".agbak-" + time.strftime("%Y%m%d-%H%M%S")
    os.rename(original_path, backup)
    try:
        os.rename(staged_path, original_path)
    except OSError:
        os.rename(backup, original_path)
        raise
    return backup


def cleanup_stage(stage_dir):
    """Удаляет временную папку staging'а (игнорирует ошибки)."""
    if not stage_dir:
        return
    try:
        shutil.rmtree(stage_dir, ignore_errors=True)
    except Exception:
        pass


def check_js_syntax(path):
    """Проверяет синтаксис JS через `node --check`.

    Возвращает (ok, message). Если node не установлен — проверка
    пропускается (ok=True), чтобы патчер работал и без Node.js.
    """
    node = shutil.which("node")
    if not node:
        return True, "node не найден — проверка синтаксиса пропущена"
    try:
        proc = subprocess.run(
            [node, "--check", path],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return True, f"проверка синтаксиса не выполнена ({e})"
    if proc.returncode == 0:
        return True, "синтаксис ок"
    lines = (proc.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else "синтаксическая ошибка"
    return False, detail


def write_file_atomic(path, data):
    """Записывает data в path через временный файл рядом и os.replace.

    Сохраняет права и владельца исходного файла.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            st = os.stat(path)
            os.chmod(tmp_path, st.st_mode & 0o7777)
            try:
                os.chown(tmp_path, st.st_uid, st.st_gid)
            except (OSError, AttributeError):
                pass
        except OSError:
            pass
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Правка файла внутри .app: прямая запись или staging + подмена бандла        #
# --------------------------------------------------------------------------- #

def backup_store_dir():
    """Каталог для резервных копий ВНЕ .app-бандлов.

    Любой новый файл внутри .app после подписи ломает codesign-печать —
    macOS считает приложение повреждённым. Поэтому бэкапы main.js и
    журналы правок хранятся в отдельном хранилище.
    """
    home = get_posix_invoking_user_home() or os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        path = os.path.join(base, "ag_patcher", "backups")
    elif sys.platform == "darwin":
        path = os.path.join(home, "Library", "Application Support", "ag_patcher", "backups")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        path = os.path.join(base, "ag_patcher", "backups")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return ""
    return path


def app_bundle_of(path):
    """Возвращает корень .app-бандла для path или '' (если не внутри бандла)."""
    if not path:
        return ""
    p = os.path.abspath(path)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return ""


def patch_file_in_bundle(path, data, backup_suffix=".agbak"):
    """Записывает data в path, обходя App Management на macOS.

    Логика:
      * файл вне .app — обычная атомарная запись;
      * бандл доступен для записи — тоже обычная запись;
      * бандл защищён (macOS App Management) — копия бандла в /private/tmp,
        правка в копии, переподпись и подмена целиком через rename.

    Возвращает dict: {'method': 'direct'|'staged'|'copy', 'backup': path,
    'staged': staged_path, 'stage_dir': dir}
    """
    app_path = app_bundle_of(path)

    if not app_path:
        write_file_atomic(path, data)
        return {"method": "direct", "backup": "", "staged": "", "stage_dir": ""}

    if bundle_writable(app_path):
        write_file_atomic(path, data)
        return {"method": "direct", "backup": "", "staged": "", "stage_dir": ""}

    staged, stage_dir = stage_bundle(app_path)
    rel = os.path.relpath(os.path.abspath(path), app_path)
    staged_file = os.path.join(staged, rel)
    try:
        with open(staged_file, "rb") as f:
            original = f.read()
    except OSError:
        original = None
    if original is not None:
        with open(staged_file + backup_suffix, "wb") as f:
            f.write(original)
    write_file_atomic(staged_file, data)
    return {"method": "staged", "backup": "", "staged": staged,
            "stage_dir": stage_dir, "staged_file": staged_file,
            "rel": rel, "app_path": app_path}


