import os
import sys
import webbrowser
import locale

from patcher.constants import (
    COLOR_CYAN,
    COLOR_GREEN,
    COLOR_YELLOW,
    COLOR_RED,
    COLOR_BOLD,
    COLOR_DIM,
    COLOR_WHITE,
    COLOR_UNDERLINE,
    DOWNLOAD_URL,
    VERSION,
)
from patcher.utils.console import (
    color,
    link,
    clear_screen,
    print_banner,
    print_menu_section,
    print_menu_row,
    print_menu_divider,
    print_menu_footer,
    info,
    hint,
    ok,
    warn,
    err,
    cancel,
)

from patcher.utils.file import file_size, format_bytes
from patcher.utils.update import check_for_updates, check_for_updates_async, open_releases_page, print_update_status_notice

from patcher.ide.discovery import (
    find_install_root,
    find_main_js,
    get_ag_version,
    resolve_target_path,
)
from patcher.ide.patcher import is_already_patched, do_patch, do_restore
from patcher.agy.discovery import (
    find_agy_binary,
    find_ide_backend_binary,
    resolve_agy_path,
)
from patcher.agy.patcher import (
    is_already_patched as is_agy_patched,
    do_patch_agy,
    do_restore_agy,
)

from patcher.manager.discovery import find_manager_binary, resolve_manager_path, get_antigravity_version
from patcher.manager.patcher import is_already_patched as is_mgr_patched, do_patch_manager, do_restore_manager

from patcher.vscode.discovery import (
    find_extension_js,
    resolve_extension_path,
    find_gemini_antigravity_binary,
    describe_gemini_binary_path,
)
from patcher.vscode.patcher import (
    is_already_patched as is_vscode_patched,
    do_patch_vscode,
    do_restore_vscode,
)


def pause(prompt="  Press Enter to return to menu..."):
    print(color(prompt, COLOR_DIM), end="", flush=True)
    if os.name == "nt":
        import msvcrt
        try:
            while msvcrt.kbhit():
                msvcrt.getch()
            while True:
                ch = msvcrt.getch()
                if ch in (b"\r", b"\n"):
                    break
        except Exception:
            input()
    else:
        try:
            import tty
            import termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            input()
    print()



def print_launch_examples():
    script_name = os.path.basename(sys.argv[0]) or "main.py"
    cmd = script_name if getattr(sys, "frozen", False) else f"python {script_name}"
    windows_example = f'{cmd} "C:\\Path\\To\\Antigravity IDE"'
    macos_example = f'{cmd} "/Applications/Antigravity IDE.app"'
    linux_example = f'{cmd} "/usr/share/antigravity-ide"'

    hint("Usage examples with custom path:")
    print(f"      Windows: {color(windows_example, COLOR_YELLOW)}")
    print(f"      macOS:   {color(macos_example, COLOR_YELLOW)}")
    print(f"      Linux:   {color(linux_example, COLOR_YELLOW)}")


def print_path_examples():
    windows_path = r"C:\Users\Name\AppData\Local\Programs\Antigravity IDE"
    macos_path = "/Applications/Antigravity IDE.app"
    linux_path = "/usr/share/antigravity-ide"

    hint("Path examples:")
    print(f"      Windows: {color(windows_path, COLOR_YELLOW)}")
    print(f"      macOS:   {color(macos_path, COLOR_YELLOW)}")
    print(f"      Linux:   {color(linux_path, COLOR_YELLOW)}")


def _read_console_line(prompt):
    print(prompt, end="", flush=True)

    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        return sys.stdin.readline().rstrip("\r\n")

    raw = stdin_buffer.readline()
    if not raw:
        return ""

    encodings = [
        sys.stdin.encoding,
        locale.getpreferredencoding(False),
        "utf-8",
        "cp1251",
        "latin-1",
    ]
    for encoding in [e for e in encodings if e]:
        try:
            return raw.decode(encoding).rstrip("\r\n")
        except UnicodeDecodeError:
            pass

    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def prompt_yn(question):
    question = question.rstrip()
    prompt = f"  [?] {question} ({color('y', COLOR_GREEN)}/{color('n', COLOR_RED)}): "
    return _read_console_line(prompt).strip().lower()


def confirmed(question):
    """Возвращает True, если пользователь ответил 'y'."""
    return prompt_yn(question) in ("y", "yes", "\u0434", "\u0434\u0430")


def offer_download_and_block(software_name="Antigravity", target_type=None):
    err(f"{software_name} path is not set or software is not installed.")
    err("Patching attempt blocked.")
    hint(f"Download URL: {color(DOWNLOAD_URL, COLOR_CYAN)}")
    print()
    print_menu_row("1", "Open download page in browser", DOWNLOAD_URL, COLOR_GREEN)
    print_menu_row("2", f"Specify path to installed {software_name}", "manual path selection", COLOR_CYAN)
    print_menu_row("0", "Return to main menu", "", COLOR_RED)
    print()

    action = input(color("  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip()
    if action == "1":
        webbrowser.open(DOWNLOAD_URL)
        ok(f"Opening: {color(DOWNLOAD_URL, COLOR_CYAN)}")
        return None
    elif action == "2":
        print()
        hint(f"Enter the path to {software_name}.")
        print_path_examples()
        raw = input(color(f"\n  {software_name} Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
        if raw:
            if target_type == "ide" or software_name == "Antigravity IDE":
                new_path = resolve_target_path(raw)
                if new_path and os.path.isdir(new_path):
                    new_path = find_main_js(new_path)
                if new_path and os.path.isfile(new_path) and new_path.endswith("main.js"):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target (main.js not found).")
            elif target_type == "manager" or software_name == "Antigravity 2.0":
                new_path = resolve_manager_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target (language_server not found).")
            elif target_type == "agy" or software_name == "Antigravity CLI":
                new_path = resolve_agy_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target.")
            else:
                p = resolve_target_path(raw) or resolve_manager_path(raw) or resolve_agy_path(raw)
                if p and os.path.exists(p):
                    ok("Path updated!")
                    return p
                else:
                    err("Invalid path provided.")
        return None
    else:
        cancel("Cancelled.")
        return None


def _kv(label, value_text, value_color):
    """Отформатированная пара ключ/значение с выравниванием меток."""
    print(f"      {label:<9}{color(value_text, value_color)}")


def print_target_info(main_js_path, manager_path="", agy_path="", vscode_path="",
                      vscode_agy_path="", backend_agy_path="", show_search_line=False):
    if show_search_line:
        info("Searching for installations...")

    print_menu_section("TARGET STATUS")

    # 1. Antigravity IDE Info
    if main_js_path and os.path.exists(main_js_path):
        try:
            with open(main_js_path, "r", encoding="utf-8") as f:
                content = f.read()
            patched = is_already_patched(content)
            ver_str, _ = get_ag_version(main_js_path)
            size = file_size(main_js_path)
            status_text = "already patched" if patched else "not patched"
            status_color = COLOR_GREEN if patched else COLOR_YELLOW
            icon = color("[+]", COLOR_GREEN) if patched else color("[!]", COLOR_YELLOW)
            ver_info = f" (v{ver_str})" if ver_str else ""
            size_info = f" • {format_bytes(size)}" if size > 0 else ""
            print(f"  {icon} {color('Antigravity IDE:', COLOR_WHITE):<24} {color(status_text, status_color)}{color(ver_info + size_info, COLOR_DIM)}")
            print(f"      {color(main_js_path, COLOR_DIM)}")
        except Exception:
            print(f"  {color('[-]', COLOR_RED)} {color('Antigravity IDE:', COLOR_WHITE):<24} {color('unreadable', COLOR_RED)}")
    else:
        print(f"  {color('[-]', COLOR_DIM)} {color('Antigravity IDE:', COLOR_WHITE):<24} {color('not found', COLOR_DIM)}")

    # 2. Antigravity 2.0 (language_server) Info
    if manager_path and os.path.isfile(manager_path):
        patched = is_mgr_patched(manager_path)
        ver_str = get_antigravity_version(manager_path)
        size = file_size(manager_path)
        status_text = "already patched" if patched else "not patched"
        status_color = COLOR_GREEN if patched else COLOR_YELLOW
        icon = color("[+]", COLOR_GREEN) if patched else color("[!]", COLOR_YELLOW)
        ver_info = f" (v{ver_str})" if ver_str else ""
        size_info = f" • {format_bytes(size)}" if size > 0 else ""
        print(f"  {icon} {color('Antigravity 2.0:', COLOR_WHITE):<24} {color(status_text, status_color)}{color(ver_info + size_info, COLOR_DIM)}")
        print(f"      {color(manager_path, COLOR_DIM)}")
    else:
        print(f"  {color('[-]', COLOR_DIM)} {color('Antigravity 2.0:', COLOR_WHITE):<24} {color('not found', COLOR_DIM)}")

    # 3. Antigravity CLI (agy) Info
    if agy_path and os.path.isfile(agy_path):
        patched = is_agy_patched(agy_path)
        size = file_size(agy_path)
        status_text = "already patched" if patched else "not patched"
        status_color = COLOR_GREEN if patched else COLOR_YELLOW
        icon = color("[+]", COLOR_GREEN) if patched else color("[!]", COLOR_YELLOW)
        size_info = f" ({format_bytes(size)})" if size > 0 else ""
        print(f"  {icon} {color('Antigravity CLI:', COLOR_WHITE):<24} {color(status_text, status_color)}{color(size_info, COLOR_DIM)}")
        print(f"      {color(agy_path, COLOR_DIM)}")
    else:
        print(f"  {color('[-]', COLOR_DIM)} {color('Antigravity CLI:', COLOR_WHITE):<24} {color('not found', COLOR_DIM)}")

    # 4. Antigravity IDE backend (agy --hub) Info
    backend_status = None
    if backend_agy_path and os.path.isfile(backend_agy_path):
        patched = is_agy_patched(backend_agy_path)
        backend_status = patched
        size = file_size(backend_agy_path)
        status_text = "already patched" if patched else "not patched"
        status_color = COLOR_GREEN if patched else COLOR_YELLOW
        icon = color("[+]", COLOR_GREEN) if patched else color("[!]", COLOR_YELLOW)
        size_info = f" ({format_bytes(size)})" if size > 0 else ""
        print(f"  {icon} {color('IDE Backend (agy):', COLOR_WHITE):<24} {color(status_text, status_color)}{color(size_info, COLOR_DIM)}")
        print(f"      {color(backend_agy_path, COLOR_DIM)}")
    else:
        print(f"  {color('[-]', COLOR_DIM)} {color('IDE Backend (agy):', COLOR_WHITE):<24} {color('not found', COLOR_DIM)}")

    # 5. Antigravity VS Code Extension Info (extension.js + agy bin)
    has_ext = bool(vscode_path and os.path.isfile(vscode_path))
    has_bin = bool(vscode_agy_path and os.path.isfile(vscode_agy_path))
    if has_ext or has_bin:
        ext_patched = False
        if has_ext:
            try:
                with open(vscode_path, "r", encoding="utf-8") as f:
                    ext_patched = is_vscode_patched(f.read())
            except Exception:
                pass

        if has_bin:
            if backend_status is not None and os.path.abspath(vscode_agy_path) == os.path.abspath(backend_agy_path):
                bin_patched = backend_status
            else:
                bin_patched = is_agy_patched(vscode_agy_path)
        else:
            bin_patched = False

        all_patched = (ext_patched if has_ext else True) and (bin_patched if has_bin else True)
        status_text = "already patched" if all_patched else "not patched"
        status_color = COLOR_GREEN if all_patched else COLOR_YELLOW
        icon = color("[+]", COLOR_GREEN) if all_patched else color("[!]", COLOR_YELLOW)
        print(f"  {icon} {color('VS Code Extension:', COLOR_WHITE):<24} {color(status_text, status_color)}")
        if has_ext:
            print(f"      ext: {color(vscode_path, COLOR_DIM)}")
        if has_bin and (not backend_agy_path or os.path.abspath(vscode_agy_path) != os.path.abspath(backend_agy_path)):
            print(f"      bin: {color(vscode_agy_path, COLOR_DIM)}")
    else:
        print(f"  {color('[-]', COLOR_DIM)} {color('VS Code Extension:', COLOR_WHITE):<24} {color('not found', COLOR_DIM)}")


def show_about():
    clear_screen()
    print_banner()
    print_menu_section("ABOUT OPEN AG PATCHER")
    print()
    print(f"  {color('Open AG Patcher', COLOR_BOLD, COLOR_CYAN)} v{VERSION}")
    print("  Open-source region lock bypass tool for Antigravity products:")
    print("  Antigravity IDE, Antigravity 2.0 (language_server), and Antigravity CLI (agy).")
    print("  Allows using Antigravity without VPN or changing Google account region.")
    print()
    hint("Features:")
    print(f"   • {color('Antigravity IDE patch', COLOR_GREEN)} — main.js isGoogleInternal bypass")
    print(f"   • {color('Antigravity 2.0 patch', COLOR_GREEN)} — language_server binary patch")
    print(f"   • {color('Antigravity CLI patch', COLOR_GREEN)} — agy eligibility check bypass")
    print(f"   • {color('Backup & Restore', COLOR_GREEN)}     — safe, fully reversible modifications")
    print()
    hint("Author & Community:")
    tg_main = link("https://t.me/avencoresyt", "t.me/avencoresyt", COLOR_CYAN, COLOR_UNDERLINE)
    tg_chat = link("https://t.me/avencoreschat", "t.me/avencoreschat", COLOR_CYAN, COLOR_UNDERLINE)
    yt = link("https://youtube.com/@avencores", "youtube.com/@avencores", COLOR_CYAN, COLOR_UNDERLINE)
    vk = link("https://vk.ru/avencoresreuploads", "vk.ru/avencoresreuploads", COLOR_CYAN, COLOR_UNDERLINE)
    dz = link("https://dzen.ru/avencores", "dzen.ru/avencores", COLOR_CYAN, COLOR_UNDERLINE)
    card_num = link("data:text/plain;charset=utf-8,2202%202050%201464%204675", "2202 2050 1464 4675", COLOR_BOLD, COLOR_WHITE, COLOR_UNDERLINE)
    attr_link = link("https://github.com/QNIX-Dev/eligibility-antigravity-patcher", "eligibility-antigravity-patcher", COLOR_CYAN, COLOR_UNDERLINE)

    print(f"   • Author:   {color('AvenCores', COLOR_YELLOW)}")
    print(f"   • Telegram: {tg_main} (Chat: {tg_chat})")
    print(f"   • YouTube:  {yt}")
    print(f"   • VK:       {vk}")
    print(f"   • Dzen:     {dz}")
    print()
    hint("Support Author:")
    print(f"   • {color('SBER:', COLOR_GREEN)} {card_num}")
    print()
    hint("License:")
    print(f"   • GPL-3.0 License (Attribution: {attr_link} / MIT)")



def redraw_main_screen(main_js_path, manager_path="", agy_path="", vscode_path="",
                       vscode_agy_path="", backend_agy_path="", show_search_line=False):

    clear_screen()
    print_banner()
    print_target_info(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                      vscode_agy_path=vscode_agy_path, backend_agy_path=backend_agy_path,
                      show_search_line=show_search_line)
    print()
    print_update_status_notice()


def set_vscode_path(new_path):
    """Колбэк для do_patch_vscode_flow — обновляет локальную переменную vscode_path
    в кадре run_cli (через sys._getframe)."""
    frame = sys._getframe(2)
    if frame.f_code.co_name == "run_cli":
        frame.f_locals["vscode_path"] = new_path


def set_vscode_agy_path(new_path):
    """Колбэк для do_patch_vscode_flow — обновляет локальную переменную vscode_agy_path
    в кадре run_cli (через sys._getframe)."""
    frame = sys._getframe(2)
    if frame.f_code.co_name == "run_cli":
        frame.f_locals["vscode_agy_path"] = new_path


def do_patch_vscode_flow(vscode_path, vscode_agy_path, set_js_cb=None, set_agy_cb=None):
    """Патчит расширение google.google-antigravity (extension.js) и бинарь
    ~/.gemini/bin/antigravity (agy) через agy-патчер.

    Патч блокируется, пока не найдены ОБА файла: extension.js и бинарь agy."""
    from patcher.agy.patcher import do_patch_agy

    # --- 0. Предварительный поиск: оба файла должны существовать ---
    if not vscode_path or not os.path.isfile(vscode_path):
        vscode_path = find_extension_js()
        if vscode_path and set_js_cb:
            set_js_cb(vscode_path)

    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        vscode_agy_path = find_gemini_antigravity_binary()
        if vscode_agy_path and set_agy_cb:
            set_agy_cb(vscode_agy_path)

    missing = []
    if not vscode_path or not os.path.isfile(vscode_path):
        missing.append("extension.js (VS Code extension 'google.google-antigravity')")
    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        missing.append(f"agy binary ({describe_gemini_binary_path()})")

    if missing:
        err("Antigravity VS Code Patch blocked — required files not found:")
        for m in missing:
            err(f"  - {m}")
        hint("Install the 'Google Antigravity' extension in VS Code first")
        hint("(Extensions view -> search 'Antigravity' -> Install),")
        hint("run it once — it downloads the binary to ~/.gemini/bin automatically.")
        return False

    # --- 1. Патчим extension.js ---
    do_patch_vscode(vscode_path)

    # --- 2. Патчим бинарь ~/.gemini/bin/antigravity (agy-патчем) ---
    print()
    info(f"Found downloaded binary: {color(vscode_agy_path, COLOR_CYAN)}")
    do_patch_agy(vscode_agy_path)
    return True


def run_cli():
    main_js_path = ""
    manager_path = ""
    agy_path = ""
    vscode_path = ""
    vscode_agy_path = ""
    backend_agy_path = ""
    searched = False

    # 1. Проверяем аргументы командной строки
    if len(sys.argv) > 1:
        args = [a for a in sys.argv[1:] if a not in ("--rollback", "-r")]
        if args:
            arg = " ".join(args)
            # Пытаемся определить тип цели
            ide_path = resolve_target_path(arg)
            if ide_path and os.path.isdir(ide_path):
                ide_path = find_main_js(ide_path)
            
            if ide_path and os.path.isfile(ide_path) and ide_path.endswith("main.js"):
                main_js_path = ide_path
            else:
                # Пробуем как Antigravity 2.0 (Manager / language_server)
                mgr_path = resolve_manager_path(arg)
                if mgr_path and os.path.isfile(mgr_path):
                    manager_path = mgr_path
                else:
                    # Пробуем как CLI (agy)
                    agy_p = resolve_agy_path(arg)
                    if agy_p and os.path.isfile(agy_p):
                        agy_path = agy_p
                    else:
                        err(f"Provided path does not exist or invalid: {arg}")

    # 2. Проверяем текущую директорию (для Antigravity IDE)
    if not main_js_path and not manager_path and not agy_path:
        local = os.path.join(os.getcwd(), "main.js")
        if os.path.exists(local):
            main_js_path = local
            info("Found main.js in current directory")

    # 3. Авто-поиск в системе
    if not main_js_path and not manager_path and not agy_path:
        info("Searching for installations...")
        searched = True

        ide_root = find_install_root()
        if ide_root:
            main_js_path = find_main_js(ide_root)

        manager_path = find_manager_binary()
        agy_path = find_agy_binary()

    # VS Code extension и его бинарь agy ищем всегда — независимо от того,
    # найдены ли остальные цели (это отдельный продукт со своей директорией).
    if not vscode_path or not vscode_agy_path:
        if not searched:
            info("Searching for installations...")
        if not vscode_path:
            vscode_path = find_extension_js()
        if not vscode_agy_path:
            vscode_agy_path = find_gemini_antigravity_binary()

    # Бэкенд Antigravity IDE (agy --hub) — отдельный экземпляр agy,
    # отдельный гейт: без него IDE остаётся «ineligible» даже после
    # патча main.js.
    if not backend_agy_path:
        backend_agy_path = find_ide_backend_binary()

    # Если ничего не нашли вообще, даем выбор между открытием страницы загрузки и вводом пути
    if not main_js_path and not manager_path and not agy_path:
        warn("No installations found automatically.")
        hint(f"Download URL: {color(DOWNLOAD_URL, COLOR_CYAN)}")
        print()
        print_menu_row("1", "Open download page in browser", DOWNLOAD_URL, COLOR_GREEN)
        print_menu_row("2", "Specify path to installed software", "manual path selection", COLOR_CYAN)
        print_menu_row("0", "Skip for now (go to main menu)", "", COLOR_YELLOW)
        print()

        act = input(color("  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip()
        if act == "1":
            webbrowser.open(DOWNLOAD_URL)
            ok(f"Opening: {color(DOWNLOAD_URL, COLOR_CYAN)}")
            print()
        elif act == "2":
            print()
            hint("Please specify the path to Antigravity IDE, Antigravity 2.0, or agy.")
            print_path_examples()
            raw = input(color("\n  Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
            if raw:
                ide_path = resolve_target_path(raw)
                if ide_path and os.path.isdir(ide_path):
                    ide_path = find_main_js(ide_path)
                
                if ide_path and os.path.isfile(ide_path) and ide_path.endswith("main.js"):
                    main_js_path = ide_path
                else:
                    mgr_path = resolve_manager_path(raw)
                    if mgr_path and os.path.isfile(mgr_path):
                        manager_path = mgr_path
                    else:
                        agy_p = resolve_agy_path(raw)
                        if agy_p and os.path.isfile(agy_p):
                            agy_path = agy_p

    # Auto-check for updates in background (non-blocking)
    check_for_updates_async()

    redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                       vscode_agy_path=vscode_agy_path, backend_agy_path=backend_agy_path,
                       show_search_line=searched)

    while True:
        print_menu_section("PATCH")
        print_menu_row("1", "Antigravity IDE patch", "bypass region lock (tier gate)", COLOR_GREEN)
        print_menu_row("2", "Antigravity 2.0 patch", "patch language_server binary", COLOR_GREEN)
        print_menu_row("3", "Antigravity CLI (agy) patch", "unlock agy tool", COLOR_GREEN)
        print_menu_row("4", "Antigravity IDE backend patch", "patch agy --hub binary", COLOR_GREEN)
        print_menu_row("5", "Antigravity VS Code Patch", "patch google.google-antigravity extension.js + agy binary", COLOR_GREEN)

        print_menu_section("RESTORE")
        print_menu_row("6", "Antigravity IDE", "from backup", COLOR_YELLOW)
        print_menu_row("7", "Antigravity 2.0", "from backup", COLOR_YELLOW)
        print_menu_row("8", "Antigravity CLI", "from backup", COLOR_YELLOW)
        print_menu_row("9", "Antigravity IDE backend", "from backup", COLOR_YELLOW)
        print_menu_row("10", "Antigravity VS Code extension", "from backup", COLOR_YELLOW)

        print_menu_section("TOOLS")
        print_menu_row("11", "Check for updates", "check GitHub releases", COLOR_CYAN)
        print_menu_row("12", "Open GitHub repository", "source & updates", COLOR_CYAN)
        print_menu_row("13", "Select custom path", "override auto-detected target", COLOR_CYAN)
        print_menu_row("14", "About program", "information & author links", COLOR_CYAN)

        print()
        print_menu_row("0", "Exit", "quit the patcher", COLOR_RED)
        print_menu_footer("Tip: patches are reversible — use RESTORE any time.")

        if os.name == "nt":
            import msvcrt
            try:
                while msvcrt.kbhit():
                    msvcrt.getch()
            except Exception:
                pass

        choice = input(color("\n  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip()
        print()

        if choice == "0":
            return

        # Пустой ввод — не выходим, просто перерисовываем меню
        if choice == "":
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, backend_agy_path, show_search_line=searched)
            continue

        valid_choices = {str(i) for i in range(1, 15)}
        if choice not in valid_choices:
            err("Invalid choice")
            print()
            pause()
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, backend_agy_path, show_search_line=searched)
            continue

        handled = True
        clear_screen()
        print_banner()

        if choice == "1":
            if main_js_path and os.path.isfile(main_js_path):
                do_patch(main_js_path, show_search_line=searched)
            else:
                new_p = offer_download_and_block("Antigravity IDE", target_type="ide")
                if new_p:
                    main_js_path = new_p
                    searched = False
                    do_patch(main_js_path, show_search_line=searched)
        elif choice == "2":
            if manager_path and os.path.isfile(manager_path):
                do_patch_manager(manager_path)
            else:
                new_p = offer_download_and_block("Antigravity 2.0", target_type="manager")
                if new_p:
                    manager_path = new_p
                    searched = False
                    do_patch_manager(manager_path)
        elif choice == "3":
            if agy_path and os.path.isfile(agy_path):
                do_patch_agy(agy_path)
            else:
                new_p = offer_download_and_block("Antigravity CLI", target_type="agy")
                if new_p:
                    agy_path = new_p
                    searched = False
                    do_patch_agy(agy_path)
        elif choice == "4":
            if backend_agy_path and os.path.isfile(backend_agy_path):
                do_patch_agy(backend_agy_path, is_hub=True)
            else:
                err("Antigravity IDE backend (agy --hub) not found.")
                hint("Launch Antigravity IDE once — it downloads the backend to ~/.gemini/bin.")
        elif choice == "5":
            do_patch_vscode_flow(vscode_path, vscode_agy_path,
                                 set_js_cb=set_vscode_path, set_agy_cb=set_vscode_agy_path)
        elif choice == "6":
            if main_js_path:
                do_restore(main_js_path, show_search_line=searched)
            else:
                err("Antigravity IDE path is not set. Please select custom path (Option 13) first.")
        elif choice == "7":
            if manager_path:
                do_restore_manager(manager_path)
            else:
                err("Antigravity 2.0 (language_server) path is not set. Please select custom path (Option 13) first.")
        elif choice == "8":
            if agy_path:
                do_restore_agy(agy_path)
            else:
                err("Antigravity CLI path is not set. Please select custom path (Option 13) first.")
        elif choice == "9":
            if backend_agy_path:
                do_restore_agy(backend_agy_path, is_hub=True)
            else:
                err("Antigravity IDE backend path is not set. Please select custom path (Option 13) first.")
        elif choice == "10":
            if vscode_path:
                do_restore_vscode(vscode_path)
            else:
                err("Antigravity VS Code extension path is not set. Please select custom path (Option 13) first.")
        elif choice == "11":
            check_for_updates(silent=False)
        elif choice == "12":
            print_target_info(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                              vscode_agy_path=vscode_agy_path, backend_agy_path=backend_agy_path,
                              show_search_line=searched)
            print()
            if confirmed("Open GitHub repository in browser?"):
                url = "https://github.com/AvenCores/open-antigravity-unlock"
                webbrowser.open(url)
                ok(f"Opening: {color(url, COLOR_CYAN)}")
            else:
                cancel("Cancelled.")
        elif choice == "13":
            while True:
                redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                                   vscode_agy_path, backend_agy_path, show_search_line=searched)
                print_menu_section("SELECT CUSTOM PATH")
                print_menu_row("1", "Antigravity IDE path", "folder or main.js", COLOR_GREEN)
                print_menu_row("2", "Antigravity 2.0 path", "folder or language_server binary", COLOR_GREEN)
                print_menu_row("3", "Antigravity CLI path", "agy.exe or folder", COLOR_GREEN)
                print_menu_row("4", "VS Code extension path (JS)", "extension.js or extensions folder", COLOR_GREEN)
                print_menu_row("5", "VS Code binary path (agy)", "agy/antigravity binary in ~/.gemini/bin", COLOR_GREEN)
                print_menu_row("6", "IDE backend path (agy --hub)", "binary in ~/.gemini/bin", COLOR_GREEN)
                print()
                print_menu_row("0", "Back", "return to main menu", COLOR_RED)
                print_menu_footer("Leaves auto-detection results intact for other targets.")

                sub_choice = input(color("\n  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip()
                if sub_choice == "0":
                    handled = False
                    break

                if sub_choice == "1":
                     print()
                     hint("Enter the path to Antigravity IDE folder or main.js file.")
                     print_path_examples()
                     raw = input(color("\n  IDE Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_target_path(raw)
                         if new_path and os.path.exists(new_path):
                             if os.path.isdir(new_path):
                                 new_path = find_main_js(new_path)
                             if new_path and os.path.isfile(new_path):
                                 main_js_path = new_path
                                 searched = False
                                 ok("Antigravity IDE path updated!")
                             else:
                                 err("Could not resolve a valid Antigravity IDE target (main.js not found).")
                         else:
                             err("Path does not exist.")
                     print()
                     pause()
                elif sub_choice == "2":
                     print()
                     hint("Enter the path to Antigravity 2.0 app folder or language_server binary.")
                     print_path_examples()
                     raw = input(color("\n  Antigravity 2.0 Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_manager_path(raw)
                         if new_path and os.path.isfile(new_path):
                             manager_path = new_path
                             searched = False
                             ok("Antigravity 2.0 path updated!")
                         else:
                             err("Could not resolve a valid Antigravity 2.0 target (language_server not found).")
                     print()
                     pause()
                elif sub_choice == "3":
                     print()
                     hint("Enter the path to the agy binary (agy.exe) or its folder.")
                     print_path_examples()
                     raw = input(color("\n  AGY Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_agy_path(raw)
                         if new_path and os.path.isfile(new_path):
                             agy_path = new_path
                             searched = False
                             ok("Antigravity CLI path updated!")
                         else:
                             err("Could not resolve a valid Antigravity CLI target.")
                     print()
                     pause()
                elif sub_choice == "4":
                     print()
                     hint("Enter the path to extension.js, the google.google-antigravity-*")
                     hint("extension folder, or the .vscode/extensions root folder.")
                     raw = input(color("\n  VS Code Extension Path (JS) > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_extension_path(raw)
                         if new_path and os.path.isfile(new_path):
                             vscode_path = new_path
                             searched = False
                             ok("Antigravity VS Code extension path updated!")
                         else:
                             err("Could not resolve a valid VS Code extension target (extension.js not found).")
                     print()
                     pause()
                elif sub_choice == "5":
                     print()
                     hint(f"Enter the path to the agy/antigravity binary downloaded")
                     hint(f"by the extension ({describe_gemini_binary_path()}) or any agy file.")
                     raw = input(color("\n  VS Code Binary Path (agy) > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_agy_path(raw)
                         if new_path and os.path.isfile(new_path):
                             vscode_agy_path = new_path
                             searched = False
                             ok("VS Code agy binary path updated!")
                         else:
                             err("Could not resolve a valid VS Code agy binary target.")
                     print()
                     pause()
                elif sub_choice == "6":
                     print()
                     hint("Enter the path to the Antigravity IDE backend binary (agy),")
                     hint("usually ~/.gemini/bin/agy — the one launched as 'agy --hub'.")
                     raw = input(color("\n  IDE Backend Path (agy) > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     if raw:
                         new_path = resolve_agy_path(raw)
                         if new_path and os.path.isfile(new_path):
                             backend_agy_path = new_path
                             searched = False
                             ok("Antigravity IDE backend path updated!")
                         else:
                             err("Could not resolve a valid IDE backend target.")
                     print()
                     pause()
            handled = True
        elif choice == "14":
            show_about()

        print()

        if handled:
            pause()
        redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                           vscode_agy_path, backend_agy_path, show_search_line=searched)
