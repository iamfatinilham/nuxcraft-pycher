#!/usr/bin/env python3

import os, json, subprocess, shutil, zipfile, sys, argparse, hashlib, time, uuid, multiprocessing, base64, re, datetime, shlex, platform, tarfile, glob, typing
from concurrent.futures import ThreadPoolExecutor

# Future-proof: Change this single constant if Mojang ever switches hash algorithms
HASH_ALGO = hashlib.sha1

# SMELL-101: single source of truth for reserved Windows device names (used by the
# --delete-instance validation and is_valid_instance_name)
RESERVED_NAMES = ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]

# SMELL-103: named constants for previously bare magic numbers
IO_CHUNK = 1048576                # 1 MiB streaming chunk (downloads, hashes, delta-copy) — SUG-011
CRASH_STALENESS_S = 86400         # crash logs older than 24h are not analyzed
ENABLE_VT_PROCESSING = 0x0004     # Windows console ENABLE_VIRTUAL_TERMINAL_PROCESSING
SE_PRIVILEGE_ENABLED = 0x00000002
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
LOG_MAX_BYTES = 2_000_000         # SUG-003: global launcher.log rotation
LOG_BACKUP_COUNT = 3
MANIFEST_TTL_S = 604800           # SUG-004: cached version manifest max age (7 days)

# Base64 module-level constant (SMELL-007)
def b64d(dta):
    # Opaque-constant decoder — never decode/expose these values (see AGENTS.md)
    return base64.b64decode(dta).decode('utf-8')
_GAME_ARGS_KEY = b64d("bWluZWNyYWZ0QXJndW1lbnRz")
_GAME_KEY = b64d("bWluZWNyYWZ0")

## ⚠️ Disclaimer: This project is for educational, research and testing purposes only.

############################
##### LAUNCHER VERSION #####
############################
launcher_version = "1.0"
############################

# Force UTF-8 Encoding globally to handle emojis across all OS configurations
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# Helpers (SMELL-002 & NEW-LOW-013)
def safe_makedirs(path, is_file=None):
    # is_file=True  -> path is a file; create its parent
    # is_file=False -> path is a directory; create it (bypasses dotted-dir misdetection)
    # is_file=None  -> infer from file extension (legacy behaviour)
    if not path: return
    if is_file is None:
        is_file = bool(os.path.splitext(path)[1])
    d = os.path.dirname(path) if is_file else path
    if d: os.makedirs(d, exist_ok=True)

def _read_posix_key():
    import tty, termios, select
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)  # type: ignore[attr-defined]
    try:
        tty.setraw(fd)  # type: ignore[attr-defined]
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            # Lone Esc: only read the sequence tail if it is already buffered
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)  # type: ignore[attr-defined]
    return ch

def display_paged_menu(menu_text):
    # SMELL-104: single implementation of the less -XR paged-menu fallback
    if shutil.which("less"):
        try:
            subprocess.run(["less", "-XR"], input=menu_text, text=True, check=True)
            return
        except Exception:
            pass
    print(menu_text)

_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

class _NoColorStream:
    # SUG-009: strips SGR colour sequences from stdout (NO_COLOR / --no-color).
    # Non-colour control sequences (e.g. the TUI's cursor movement) are preserved.
    def __init__(self, stream):
        self._stream = stream
    def __getattr__(self, name):
        return getattr(self._stream, name)
    def write(self, data):
        return self._stream.write(_ANSI_SGR_RE.sub("", data))
    def flush(self):
        return self._stream.flush()

# ---- PHASE 3: DELTA-COPY ENGINE ----
FICLONE = 0x40049409  # Linux ioctl for reflink copy (ext4/btrfs/xfs/zfs)

def files_identical(s_file, d_file):
    # Q19: size+mtime fast-path (copy2 preserves mtime, so our own copies always fast-path);
    # any mismatch (incl. FAT timestamp skew) falls to full SHA-1 via HASH_ALGO. Never skips wrongly.
    try:
        if os.path.getsize(s_file) != os.path.getsize(d_file): return False
        if os.path.getmtime(s_file) == os.path.getmtime(d_file): return True
    except OSError: return False
    s_h, d_h = HASH_ALGO(), HASH_ALGO()
    with open(s_file, 'rb') as sf:
        while chunk := sf.read(IO_CHUNK): s_h.update(chunk)
    with open(d_file, 'rb') as df:
        while chunk := df.read(IO_CHUNK): d_h.update(chunk)
    return s_h.hexdigest() == d_h.hexdigest()

def reflink_or_copy(s_file, d_file):
    # Q20: reflink -> copy. Zero-dependency: fcntl.ioctl on Linux, clonefile on macOS, copy2 elsewhere.
    try:
        os.makedirs(os.path.dirname(d_file) or '.', exist_ok=True)
    except OSError: pass
    if sys.platform.startswith('linux'):
        try:
            import fcntl
            with open(s_file, 'rb') as sfd, open(d_file, 'wb') as dfd:
                fcntl.ioctl(dfd.fileno(), FICLONE, sfd.fileno())  # type: ignore[attr-defined]
            shutil.copystat(s_file, d_file)
            return 'reflink'
        except (OSError, ImportError): pass
    elif is_mac:
        try:
            import ctypes
            _libc = ctypes.CDLL(None, use_errno=True)
            if hasattr(_libc, 'clonefile'):
                if _libc.clonefile(os.fsencode(s_file), os.fsencode(d_file), 0) == 0:
                    return 'reflink'
        except Exception: pass
    shutil.copy2(s_file, d_file)
    return 'copy'

def delta_copy_tree(src_root, dst_root, skip_dirs=(), hardlink_roots=(), label="Copying"):
    # Walk src; copy only changed files. skip_dirs = names excluded at any depth.
    # hardlink_roots = relative dir names whose files are treated as read-only and may hardlink (Q2 chain).
    copied = skipped = 0
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            s_file = os.path.join(root, fn)
            rel = os.path.relpath(s_file, src_root)
            d_file = os.path.join(dst_root, rel)
            if os.path.exists(d_file) and files_identical(s_file, d_file):
                skipped += 1; continue
            _hl = any(rel == hr or rel.startswith(hr + os.sep) for hr in hardlink_roots)
            if _hl:
                try:
                    os.makedirs(os.path.dirname(d_file) or '.', exist_ok=True)
                    os.link(s_file, d_file); copied += 1; continue
                except OSError: pass
            reflink_or_copy(s_file, d_file); copied += 1
    print(f"[ 🧬 ] {label}: \033[1;92m{copied}\033[0m copied / \033[1;94m{skipped}\033[0m up-to-date")
    return copied, skipped

# OS Detection
if typing.TYPE_CHECKING:
    import msvcrt
    import ctypes
    import tty
    import termios
    import fcntl

is_windows = sys.platform == "win32"
is_mac = sys.platform == "darwin"

if is_windows:
    import msvcrt, ctypes
    platform_os = "windows"
    cp_separator = ";"
    ansi_clear = False
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT_PROCESSING):
                ansi_clear = True
        else:
            ansi_clear = sys.getwindowsversion().build >= 10586
    except Exception: pass
else:
    import tty, termios
    is_freebsd = sys.platform.startswith('freebsd')
    # FreeBSD runs the game's Linux natives via the Linuxulator; map to "linux" for
    # natives classifiers and OS rules. Keep is_freebsd for FreeBSD-specific gates.
    platform_os = "osx" if is_mac else "linux"
    cp_separator = ":"
    ansi_clear = True

def has_large_pages_privilege():
    # Checks if the process actually has SeLockMemoryPrivilege enabled (required for -XX:+UseLargePages)
    if not is_windows: return False
    SE_LOCK_MEMORY_NAME = "SeLockMemoryPrivilege"
    TOKEN_QUERY = 0x0008
    try:
        # Explicit restype: GetCurrentProcess returns a pointer-sized pseudo-handle on x64
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        process = ctypes.windll.kernel32.GetCurrentProcess()
        token = ctypes.c_void_p()
        if not ctypes.windll.advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return False
            
        luid = ctypes.create_string_buffer(8)
        if not ctypes.windll.advapi32.LookupPrivilegeValueW(None, SE_LOCK_MEMORY_NAME, luid):
            ctypes.windll.kernel32.CloseHandle(token)
            return False
            
        class PRIVILEGE_SET(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.c_uint32), 
                        ("Control", ctypes.c_uint32), 
                        ("Luid", ctypes.c_uint64), 
                        ("Attributes", ctypes.c_uint32)]
                        
        ps = PRIVILEGE_SET()
        ps.PrivilegeCount = 1
        ps.Control = 0
        ctypes.memmove(ctypes.byref(ps.Luid), luid, 8)
        ps.Attributes = SE_PRIVILEGE_ENABLED
        
        result = ctypes.c_long()
        if ctypes.windll.advapi32.PrivilegeCheck(token, ctypes.byref(ps), ctypes.byref(result)):
            ctypes.windll.kernel32.CloseHandle(token)
            return result.value != 0
            
        ctypes.windll.kernel32.CloseHandle(token)
        return False
    except Exception: return False    

try:
    # Generate player UUID
    def generate_offline_uuid(username):
        # Generate player offline-mode UUIDs per MD5 spec based upon usernames.
        name = f"OfflinePlayer:{username}"
        hash_bytes = hashlib.md5(name.encode('utf-8')).digest()
        hash_list = list(hash_bytes)
        # Carefully create UUID version 3, variant 1
        hash_list[6] = (hash_list[6] & 0x0f) | 0x30 
        hash_list[8] = (hash_list[8] & 0x3f) | 0x80 
        return str(uuid.UUID(bytes=bytes(hash_list)))
    
    try:
        default_max_threads = multiprocessing.cpu_count()
    except NotImplementedError:
        default_max_threads = 4
    
    parser = argparse.ArgumentParser(description=f"  NuxCraft-PyCher ({platform_os}) Version: {launcher_version}")
    parser.add_argument("--version", action="version", version=f"NuxCraft-PyCher {launcher_version} ({platform_os})")
    parser.add_argument("-f", "--fullscreen", action="store_true", help="  Launch the game in fullscreen mode")
    parser.add_argument("--java", type=str, metavar="PATH(BINARY FULL_PATH)", default="java", help="  Java binary path")
    parser.add_argument("--game-dir", type=str, metavar="PATH(DIRECTORY FULL_PATH)", default=".game", help="  Custom game directory | Default: .game")
    parser.add_argument("-O", "--old", action="store_true", dest="old_compatibility", help="  For old version compatibility")
    parser.add_argument("-s", "--snapshots", action="store_true", dest="snapshots", help="  Show snapshot releases")
    parser.add_argument("-b", "--beta", action="store_true", dest="beta", help="  Show old beta releases")
    parser.add_argument("-R", "--refresh", action="store_true", dest="refresh", help="  Fetch version list from internet")
    parser.add_argument("-r", "--recheck", action="store_true", dest="recheck", help="  Recheck Files")
    parser.add_argument("-p", "--player", type=str, metavar="NAME", default="player", help="  Set player username | Default: player")
    parser.add_argument("-m", "--memory", type=str, dest="memory", metavar="AMOUNT", default="2G", help="  RAM (e.g. 8G) | Default: 2G")
    parser.add_argument("-t", "--threads", type=int, dest="threads", metavar="NUMBER", default=default_max_threads, help=f"  Allocate max number of threads (e.g. 4) | Default: {default_max_threads} (capped at CPU count when not set explicitly)")
    parser.add_argument("--last", "--offline", action="store_true", dest="offline", help="  Launch last version instantly")
    parser.add_argument("--jvm-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for JVM when launching game")
    parser.add_argument("--game-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for the game when launching game")
    parser.add_argument("--download-only", action="store_true", dest="game_download_only", help="  Only Download game files.")
    parser.add_argument("--cj", "--check-java", action="store_true", dest="check_java", help="  Check required Java version and exit")
    parser.add_argument("--demo", "--demo-mode", action="store_true", dest="demo_mode", help="  Launch the game in demo mode")
    parser.add_argument("--auto-install", action="store_true", dest="auto_install", help="  Automatically install missing dependencies")
    parser.add_argument("--isolate-assets", action="store_true", dest="isolate_assets", help="  Copy assets and libraries into the instance folder for standalone portability")
    parser.add_argument("--system-java", action="store_true", dest="system_java", help="  Force use of system java binary instead of auto-downloading Adoptium")
    parser.add_argument("--profile", type=str, metavar="NAME", help="  Specify an instance profile to load (e.g. 'Shader Mode')")
    
    # Platform-specific flags (exposed everywhere but may no-op)
    parser.add_argument("--no-openal", action="store_true", dest="force_disable_openal", help="  Force disable use of openal if possible (Linux)")
    parser.add_argument("--openal", action="store_true", dest="force_openal", help="  Use of openal if possible (Linux)")
    parser.add_argument("--dhp", "--disable-huge-pages", action="store_true", dest="disable_huge_pages", help="  Disable Transparent Huge Pages (Linux)")
    parser.add_argument("--dlp", "--disable-large-pages", action="store_true", dest="disable_large_pages", help="  Disable Large Pages (Windows)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="  Simulate launch: build JVM command and print it without downloading or launching")
    parser.add_argument("--delete-instance", type=str, metavar="NAME", help="  Recursively deletes the specified instance directory and its contents")
    # Non-interactive mode (Phase 1) — additive flags
    parser.add_argument("-y", "--yes", action="store_true", dest="assume_yes", help="  Assume defaults / accept prompts automatically (non-interactive mode)")
    parser.add_argument("--instance", type=str, metavar="NAME", help="  Select an instance by name (non-interactive); error if missing")
    parser.add_argument("--list-instances", action="store_true", dest="list_instances_flag", help="  Print all instances and exit")
    parser.add_argument("--list-versions", action="store_true", dest="list_versions_flag", help="  Print available game versions and exit (respects -s/-b/-R)")
    parser.add_argument("--create-instance", type=str, metavar="NAME", help="  Headlessly create an instance (requires --mc-version)")
    parser.add_argument("--loader", type=str, choices=["vanilla", "fabric", "quilt", "forge", "neoforge"], default="vanilla", help="  Mod loader for --create-instance | Default: vanilla")
    parser.add_argument("--mc-version", type=str, metavar="VER", help="  Game version ID for --create-instance (e.g. 1.20.1)")
    # Phase 4/5 - additive portability + snapshot flags
    parser.add_argument("--clone-instance", type=str, metavar="SRC", help="  Clone an existing instance (prompts for new name; or use --clone-as)")
    parser.add_argument("--clone-as", type=str, metavar="NAME", help="  Target name for --clone-instance (non-interactive)")
    parser.add_argument("--export-instance", type=str, metavar="NAME", help="  Export an instance to a portable .nuxpack archive")
    parser.add_argument("--export-with-saves", action="store_true", dest="export_saves", help="  Include saves/ in the export")
    parser.add_argument("--export-full", action="store_true", dest="export_full", help="  Mega-export: bundle client jar, natives, libraries and assets")
    parser.add_argument("--import-instance", type=str, metavar="PATH", help="  Import a .nuxpack archive as a new instance")
    parser.add_argument("--snapshot", type=str, metavar="NAME", help="  Create a full snapshot of an instance now")
    parser.add_argument("--list-snapshots", type=str, metavar="NAME", help="  List snapshots for an instance")
    parser.add_argument("--restore-snapshot", type=str, metavar="NAME", nargs='?', const="__latest__", help="  Restore a snapshot (latest, or --snapshot-file F)")
    parser.add_argument("--snapshot-file", type=str, metavar="PATH", help="  Specific snapshot archive for --restore-snapshot")
    # Offline Skin/Cape (PARITY-001) — additive flags
    parser.add_argument("--set-skin", type=str, metavar="PATH", help="  Apply a custom skin PNG (reborns a Vanilla instance into a modded one if needed)")
    parser.add_argument("--set-cape", type=str, metavar="PATH", help="  Apply a custom cape PNG (reborns a Vanilla instance into a modded one if needed)")
    # SUG-008/009/010 — CLI parity flags (additive)
    parser.add_argument("--print-cmd", action="store_true", dest="print_cmd", help="  Print the final JVM command and exit (sterile: implies --dry-run)")
    parser.add_argument("--no-color", action="store_true", dest="no_color", help="  Disable coloured output (also honours the NO_COLOR environment variable)")
    parser.add_argument("--selftest", action="store_true", dest="selftest", help="  Run internal self-tests and exit (no network, no writes)")
    
    args = parser.parse_args()
    
    # Non-interactive detection (Q17): not a TTY on either stream -> assume defaults
    INTERACTIVE = sys.stdin.isatty() and sys.stdout.isatty()
    ASSUME_YES = args.assume_yes or not INTERACTIVE
    # Structured exit codes (Q18): new paths only; legacy paths keep 0/1
    EXIT_USAGE, EXIT_NETWORK, EXIT_FILES, EXIT_JAVA, EXIT_INSTANCE = 2, 3, 4, 5, 6
    
    # SUG-008: --print-cmd is a sterile mode (never downloads or launches)
    if args.print_cmd:
        args.dry_run = True
    # SUG-009: honour --no-color / the NO_COLOR environment variable
    if args.no_color or os.environ.get("NO_COLOR"):
        sys.stdout = _NoColorStream(sys.stdout)
    # SUG-013: sterile modes never write to disk (logs/dirs/markers gated below)
    STERILE = args.dry_run or args.check_java or args.list_instances_flag or args.list_versions_flag or args.selftest or args.print_cmd
    
    if args.threads <= 0:
        print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid thread count specified: {args.threads}. Must be a positive integer.")
        sys.exit(1)

    missing_deps = []
    try:
        import requests
    except ImportError:
        missing_deps.append("requests")
        
    try:
        import tqdm
    except ImportError:
        missing_deps.append("tqdm")

    try:
        import questionary
    except ImportError:
        missing_deps.append("questionary")
        
    if missing_deps:
        if args.auto_install:
            print(f"[ ⚠️ ] \033[1;96m{' and '.join(missing_deps)}\033[0m not found. Auto-installing dependencies...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing_deps)
            except Exception as e:
                print(f"[ ❌ ] \033[1;91mFailed to install dependencies automatically:\033[0m {e}")
                print(f"       Please run: \033[1;96m{sys.executable} -m pip install {' '.join(missing_deps)}\033[0m manually.")
                sys.exit(1)
        else:
            print(f"[ ⚠️ ] \033[1;93mMissing dependencies:\033[0m \033[1;96m{', '.join(missing_deps)}\033[0m")
            if ASSUME_YES:
                ans = "y"
            else:
                try:
                    ans = input("    Do you want to install them now? [Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print(f"\n[ ❌ ] \033[1;91mCannot proceed without required dependencies. Exiting...\033[0m\n")
                    sys.exit(1)
            if ans in ('', 'y', 'yes'):
                print(f"[ ⏳ ] \033[1;97mInstalling...\033[0m")
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing_deps)
                except Exception as e:
                    print(f"\n[ ❌ ] \033[1;91mInstallation failed:\033[0m {e}")
                    print(f"       Please run: \033[1;96m{sys.executable} -m pip install {' '.join(missing_deps)}\033[0m manually.\n")
                    sys.exit(1)
            else:
                print(f"\n[ ❌ ] \033[1;91mCannot proceed without required dependencies. Exiting...\033[0m\n")
                sys.exit(1)
                
    import requests
    from tqdm import tqdm
    import questionary
    
    
    args.threads = min(args.threads, default_max_threads)
    
    # Useful vars (all of them generated on the fly) [better not to edit them]
    def get_valid_username(raw_name):
        def is_valid(name):
            return bool(re.match(r'^[a-zA-Z0-9_]{1,16}$', name))
            
        if is_valid(raw_name):
            return raw_name
            
        sanitized = re.sub(r'[^a-zA-Z0-9_]', '', raw_name)[:16] or "player"
        print(f"\n[ ⚠️ ] \033[1;93mWarning:\033[0m Username '\033[1;97m{raw_name}\033[0m' is invalid. Usernames must be 1-16 characters long and contain only letters, numbers, or underscores ([a-zA-Z0-9_]).")
        
        if not sys.stdin.isatty():
            print(f"[ ℹ️ ] Non-interactive mode: Auto-sanitized username to '\033[1;92m{sanitized}\033[0m'.")
            return sanitized
            
        current = raw_name
        while not is_valid(current):
            try:
                if 'questionary' in sys.modules:
                    current = questionary.text(
                        "Enter a valid username (1-16 chars, letters/numbers/_):",
                        default=sanitized
                    ).ask()
                    if current is None:
                        print("\n[ 💀 ] Shutdown requested by user. BYE...")
                        sys.exit(0)
                else:
                    current = input("Enter a valid username (1-16 chars, letters/numbers/_): ").strip()
            except KeyboardInterrupt:
                print("\n[ 💀 ] Shutdown requested by user. BYE...")
                sys.exit(0)
            except Exception:
                current = sanitized
                
            if not current:
                current = "player"
                
            if not is_valid(current):
                print(f"\n[ ❌ ] \033[1;91m'{current}' is invalid. Please use only letters, numbers, or underscores (1-16 chars).\033[0m\n")
                
        return current

    USERNAME = get_valid_username(args.player)
    UUID = generate_offline_uuid(USERNAME)
    MC_DIR = os.path.abspath(args.game_dir)
    
    if args.delete_instance:
        target_inst = args.delete_instance
        _instances_root = os.path.abspath(os.path.join(MC_DIR, "instances"))
        inst_path = os.path.abspath(os.path.join(_instances_root, target_inst))
        if (not target_inst or len(target_inst) > 64
                or re.search(r'[\\/:*?"<>|]', target_inst)
                or target_inst.strip('.') == '' or '..' in target_inst
                or target_inst != target_inst.rstrip('. ')
                or target_inst.upper() in RESERVED_NAMES
                or target_inst.split('.')[0].upper() in RESERVED_NAMES
                or not (inst_path == _instances_root or inst_path.startswith(_instances_root + os.sep))
                or os.path.basename(inst_path) != target_inst):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid instance name: '{target_inst}'.")
            sys.exit(1)
        if not os.path.isdir(inst_path):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{target_inst}' does not exist.")
            sys.exit(1)
            
        print(f"\n[ ⚠️ ] \033[1;93mWARNING: You are about to PERMANENTLY DELETE the instance '{target_inst}' and all its contents (saves, mods, etc).\033[0m")
        if args.assume_yes:
            # SUG-113: explicit -y enables scriptable deletion. Skip the interactive
            # re-type confirmation; the name/path validation above still applies.
            confirm = target_inst
            print(f"[ ℹ️ ] \033[1;96mNon-interactive mode (-y): auto-confirming deletion of '\033[1;97m{target_inst}\033[0m\033[1;96m'.\033[0m")
        else:
            try:
                confirm = input(f"       Type the exact name of the instance ('{target_inst}') to confirm deletion: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[ 💀 ] Shutdown requested by user. BYE...")
                sys.exit(0)
        if confirm == target_inst:
            try:
                shutil.rmtree(inst_path)
                print(f"[ ✅ ] \033[1;92mInstance '{target_inst}' has been completely deleted.\033[0m")
            except Exception as e:
                print(f"[ ❌ ] \033[1;91mError deleting instance:\033[0m {e}")
                sys.exit(1)
        else:
            print("[ ℹ️ ] Deletion aborted.")
        sys.exit(0)
    
    # ---- PHASE 4: CLONE / EXPORT / IMPORT (+ PHASE 5 snapshot dispatch, CB-5.2) ----
    def _inst_root(name): return os.path.join(MC_DIR, "instances", name)
    
    def _nuxpack_manifest(inst_name, with_saves, full):
        return {
            "format": "nuxpack", "formatVersion": 1,
            "launcher": f"NuxCraft-PyCher {launcher_version}",
            "created": datetime.datetime.now().isoformat(timespec='seconds'),
            "instance": {"name": inst_name, "mc_version": get_instance_version(inst_name) or "unknown"},
            "options": {"saves": bool(with_saves), "full": bool(full)}
        }
    
    def export_instance(name, with_saves, full):
        src = _inst_root(name)
        if not os.path.isdir(src):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{name}' does not exist."); sys.exit(EXIT_INSTANCE)
        out_dir = os.path.join(MC_DIR, "exports"); os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{name}.nuxpack")
        print(f"[ 📦 ] Exporting '\033[1;97m{name}\033[0m\033[1;94m' -> {out_path}...\033[0m")
        # full mode: stage isolated libraries/assets + version jars via delta engine first
        _staging = None
        if full:
            _staging = os.path.join(MC_DIR, "cache", "export_staging", name)
            shutil.rmtree(_staging, ignore_errors=True)
            if os.path.isdir(os.path.join(MC_DIR, "libraries")):
                delta_copy_tree(os.path.join(MC_DIR, "libraries"), os.path.join(_staging, "libraries"), label="Staging libraries")
            if os.path.isdir(os.path.join(MC_DIR, "assets")):
                delta_copy_tree(os.path.join(MC_DIR, "assets"), os.path.join(_staging, "assets"), label="Staging assets")
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr("nuxpack.json", json.dumps(_nuxpack_manifest(name, with_saves, full), indent=2))
            _include = ('mods', 'config', 'resourcepacks', 'shaderpacks') + (('saves',) if with_saves else ())
            for _sub in _include:
                _d = os.path.join(src, _sub)
                if not os.path.isdir(_d): continue
                for root, dirs, files in os.walk(_d):
                    for fn in files:
                        _f = os.path.join(root, fn)
                        z.write(_f, os.path.join("instance", _sub, os.path.relpath(_f, _d)).replace(os.sep, "/"))
            for _f in ("profiles.json", ".primary_version", "options.txt", "servers.dat"):
                if os.path.exists(os.path.join(src, _f)): z.write(os.path.join(src, _f), os.path.join("instance", _f).replace(os.sep, "/"))
            _vd = os.path.join(src, "versions")
            if os.path.isdir(_vd):
                for root, dirs, files in os.walk(_vd):
                    for fn in files:
                        if not full and not fn.endswith('.json'): continue  # metadata only unless --export-full
                        if not full and os.sep + "natives" + os.sep in root: continue
                        _f = os.path.join(root, fn)
                        z.write(_f, os.path.join("instance", "versions", os.path.relpath(_f, _vd)).replace(os.sep, "/"))
            if full and _staging and os.path.isdir(_staging):
                for _sub in ("libraries", "assets"):
                    _d = os.path.join(_staging, _sub)
                    if not os.path.isdir(_d): continue
                    for root, dirs, files in os.walk(_d):
                        for fn in files:
                            _f = os.path.join(root, fn)
                            z.write(_f, os.path.join("shared", _sub, os.path.relpath(_f, _d)).replace(os.sep, "/"))
        if _staging: shutil.rmtree(_staging, ignore_errors=True)
        print(f"[ ✅ ] Export complete: \033[1;92m{out_path}\033[0m ({os.path.getsize(out_path)//1024} KiB)")
    
    def import_instance(archive_path):
        if not os.path.isfile(archive_path):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Archive not found: {archive_path}"); sys.exit(EXIT_FILES)
        _stage = os.path.join(MC_DIR, "cache", "import_staging")
        shutil.rmtree(_stage, ignore_errors=True); os.makedirs(_stage, exist_ok=True)
        _stage_abs = os.path.abspath(_stage)
        try:
            with zipfile.ZipFile(archive_path, 'r') as z:
                for _m in z.namelist():
                    _mp = os.path.abspath(os.path.join(_stage, _m))
                    if not (_mp == _stage_abs or _mp.startswith(_stage_abs + os.sep)):
                        raise Exception(f"Attempted path traversal in archive: {_m}")
                z.extractall(_stage)
        except Exception as e:
            print(f"[ ❌ ] \033[1;91mImport failed (bad archive):\033[0m {e}"); sys.exit(EXIT_FILES)
        _mp = os.path.join(_stage, "nuxpack.json")
        if not os.path.exists(_mp):
            print(f"[ ❌ ] \033[1;91mNot a valid nuxpack archive (missing manifest).\033[0m"); sys.exit(EXIT_FILES)
        try:
            with open(_mp, 'r', encoding='utf-8') as f: _meta = json.load(f)
            if not isinstance(_meta, dict): raise ValueError("manifest is not a JSON object")
        except Exception:
            print(f"[ ❌ ] \033[1;91mCorrupt nuxpack manifest.\033[0m"); sys.exit(EXIT_FILES)
        _inst_meta = _meta.get("instance")
        if not isinstance(_inst_meta, dict): _inst_meta = {}
        _name = _inst_meta.get("name") or os.path.splitext(os.path.basename(archive_path))[0]
        if not is_valid_instance_name(_name):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid instance name in archive: '{_name}'."); sys.exit(EXIT_USAGE)
        if _name in list_instances():
            if ASSUME_YES:
                _base = _name; _n = 1
                while f"{_base}_imported{_n if _n > 1 else ''}" in list_instances(): _n += 1
                _name = f"{_base}_imported" + (str(_n) if _n > 1 else "")
                print(f"[ ℹ️ ] Name collision; importing as '\033[1;97m{_name}\033[0m'.")
            else:
                while True:
                    try:
                        _new = input(f"    Instance '{_name}' exists. New name (blank = abort): ").strip()
                    except (EOFError, KeyboardInterrupt): _new = ""
                    if not _new: print("[ ℹ️ ] Import aborted."); sys.exit(0)
                    if is_valid_instance_name(_new) and _new not in list_instances(): _name = _new; break
                    print("[ ❌ ] \033[1;91mInvalid or taken name.\033[0m")
        create_instance_dirs(_name)
        _dst = _inst_root(_name); _dst_abs = os.path.abspath(_dst)
        _inst_stage = os.path.join(_stage, "instance")
        if os.path.isdir(_inst_stage):
            for root, dirs, files in os.walk(_inst_stage):
                for fn in files:
                    _f = os.path.join(root, fn)
                    _rel = os.path.relpath(_f, _inst_stage)
                    _t = os.path.join(_dst, _rel)
                    if not (os.path.abspath(_t) == _dst_abs or os.path.abspath(_t).startswith(_dst_abs + os.sep)):
                        continue
                    os.makedirs(os.path.dirname(_t), exist_ok=True); shutil.move(_f, _t)
        # full packs: restore shared libraries/assets into the global dirs (dedup via delta engine)
        for _sub in ("libraries", "assets"):
            _d = os.path.join(_stage, "shared", _sub)
            if os.path.isdir(_d):
                delta_copy_tree(_d, os.path.join(MC_DIR, _sub), label=f"Restoring {_sub}")
        shutil.rmtree(_stage, ignore_errors=True)
        print(f"[ ✅ ] Imported instance '\033[1;92m{_name}\033[0m' (launch once to verify/fill any missing files).")
    
    def clone_instance(src_name, new_name):
        src = _inst_root(src_name)
        if not os.path.isdir(src):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{src_name}' does not exist."); sys.exit(EXIT_INSTANCE)
        if not new_name:
            if ASSUME_YES:
                print(f"[ ❌ ] \033[1;91m--clone-instance requires --clone-as in non-interactive mode.\033[0m"); sys.exit(EXIT_USAGE)
            while True:
                try:
                    new_name = input(f"    Clone '{src_name}' as (blank = abort): ").strip()
                except (EOFError, KeyboardInterrupt): new_name = ""
                if not new_name: print("[ ℹ️ ] Clone aborted."); sys.exit(0)
                if is_valid_instance_name(new_name) and new_name not in list_instances(): break
                print("[ ❌ ] \033[1;91mInvalid or taken name.\033[0m")
        if not is_valid_instance_name(new_name) or new_name in list_instances():
            print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid or existing target name '{new_name}'."); sys.exit(EXIT_USAGE)
        create_instance_dirs(new_name)
        delta_copy_tree(src, _inst_root(new_name), skip_dirs=('logs', 'crash-reports'),
                        hardlink_roots=('versions', 'libraries', 'assets'), label=f"Cloning '{src_name}'")
        for _f in glob.glob(os.path.join(_inst_root(new_name), "hs_err_pid*.log")):
            try: os.remove(_f)
            except OSError: pass
        print(f"[ ✅ ] Cloned '\033[1;97m{src_name}\033[0m' -> '\033[1;92m{new_name}\033[0m'.")
    
    # Phase 4 Backup System Initialization (+ Phase 5 snapshot keys, CB-5.1).
    # Placed BEFORE the dispatch lines so MAX_SNAPSHOTS exists when --snapshot runs.
    if not STERILE:
        os.makedirs(MC_DIR, exist_ok=True)  # fresh --game-dir: config write needs the root to exist
    config_path = os.path.join(MC_DIR, "nuxcraft_config.json")
    if not os.path.exists(config_path) and not STERILE:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"max_backups": 20, "auto_snapshot_saves": True, "max_snapshots": 10}, f, indent=4)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        max_backups = int(cfg.get("max_backups", 20))
    except (TypeError, ValueError):
        max_backups = 20
    _auto_ss = cfg.get("auto_snapshot_saves", True)
    AUTO_SNAPSHOT_SAVES = bool(_auto_ss)
    try:
        MAX_SNAPSHOTS = int(cfg.get("max_snapshots", 10))
    except (TypeError, ValueError):
        MAX_SNAPSHOTS = 10
    
    # ---- PHASE 5: SNAPSHOTS ----
    def _snap_dir(name): return os.path.join(MC_DIR, "backups", name)
    
    def create_snapshot(name, kind='full'):
        src = _inst_root(name)
        if not os.path.isdir(src):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{name}' does not exist."); sys.exit(EXIT_INSTANCE)
        os.makedirs(_snap_dir(name), exist_ok=True)
        _sub = 'saves' if kind == 'saves' else None
        if _sub and not os.path.isdir(os.path.join(src, _sub)):
            return None  # nothing to snapshot yet (no saves dir)
        _ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        _zip = os.path.join(_snap_dir(name), f"snapshot-{kind}-{_ts}.zip")
        with zipfile.ZipFile(_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            _walk_root = os.path.join(src, _sub) if _sub else src
            for root, dirs, files in os.walk(_walk_root):
                dirs[:] = [d for d in dirs if d not in ('logs', 'crash-reports')]
                for fn in files:
                    if kind == 'full' and fn.startswith('hs_err_pid'): continue
                    _f = os.path.join(root, fn)
                    z.write(_f, os.path.relpath(_f, src).replace(os.sep, "/"))  # arcnames relative to instance root, ZIP-slash normalized
        prune_snapshots(name)
        return _zip
    
    def prune_snapshots(name):
        _z = sorted(glob.glob(os.path.join(_snap_dir(name), "snapshot-*.zip")), key=os.path.getmtime)
        while len(_z) > MAX_SNAPSHOTS:
            try: os.remove(_z.pop(0))
            except OSError: break
    
    def list_snapshots(name):
        _z = sorted(glob.glob(os.path.join(_snap_dir(name), "snapshot-*.zip")), key=os.path.getmtime, reverse=True)
        if not _z: print(f"[ ℹ️ ] No snapshots for '{name}'."); return []
        print(f"\n\033[1;96m  ---- SNAPSHOTS: {name} ----\033[0m")
        for _i, _f in enumerate(_z):
            print(f"    \033[1;96m{_i+1}\033[0m. \033[1;97m{os.path.basename(_f)}\033[0m  ({os.path.getsize(_f)//1024} KiB)")
        return _z
    
    def restore_snapshot(name, archive):
        _z = sorted(glob.glob(os.path.join(_snap_dir(name), "snapshot-*.zip")), key=os.path.getmtime, reverse=True)
        if archive in (None, "__latest__"):
            if not _z: print(f"[ ❌ ] \033[1;91mNo snapshots for '{name}'.\033[0m"); sys.exit(EXIT_FILES)
            archive = _z[0]
        if not os.path.isfile(archive):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Snapshot not found: {archive}"); sys.exit(EXIT_FILES)
        if not ASSUME_YES:
            print(f"\n[ ⚠️ ] \033[1;93mWARNING: restoring will OVERWRITE instance files from '{os.path.basename(archive)}'.\033[0m")
            try:
                _c = input(f"       Type the instance name '{name}' to confirm: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[ 💀 ] Shutdown requested by user. BYE..."); sys.exit(0)
            if _c != name: print("[ ℹ️ ] Restore aborted."); sys.exit(0)
        _dst = _inst_root(name); _dst_abs = os.path.abspath(_dst)
        try:
            with zipfile.ZipFile(archive, 'r') as zf:
                for _m in zf.namelist():
                    _mp = os.path.abspath(os.path.join(_dst, _m))
                    if not (_mp == _dst_abs or _mp.startswith(_dst_abs + os.sep)):
                        raise Exception(f"Attempted path traversal in snapshot: {_m}")
                # SUG-108: unlink existing files before extraction so a restore over a
                # clone's hardlinked files never mutates the source instance in place.
                for _m in zf.namelist():
                    _mp = os.path.join(_dst, _m)
                    if os.path.isfile(_mp):
                        try: os.remove(_mp)
                        except OSError: pass
                zf.extractall(_dst)
        except Exception as e:
            print(f"[ ❌ ] \033[1;91mRestore failed:\033[0m {e}"); sys.exit(EXIT_FILES)
        print(f"[ ✅ ] Restored '\033[1;92m{name}\033[0m' from {os.path.basename(archive)}.")
    
    
    MEMORY = args.memory
    MAX_THREAD_COUNT = args.threads
    JVM_ARGS = args.jvm_flags
    GAME_ARGS = args.game_flags
    DEMO_MODE = args.demo_mode
    
    if not STERILE:
        for folder in ['instances', 'libraries', 'assets/indexes', 'assets/objects', 'resources', 'cache', 'logs', 'backups']:
            safe_makedirs(os.path.join(MC_DIR, folder))
    
    # INSTANCE MANAGEMENT UTILITIES
    INSTANCE_DIRS = ['mods', 'config', 'saves', 'resourcepacks', 'shaderpacks', 'screenshots', 'logs']
    
    def is_valid_instance_name(name):
        if not name or len(name) > 64: return False
        if re.search(r'[\\/:*?"<>|]', name): return False
        if name.strip('.') == '' or '..' in name: return False
        if name != name.rstrip('. '): return False  # Win32 strips trailing dots/spaces
        if name.upper() in RESERVED_NAMES or name.split('.')[0].upper() in RESERVED_NAMES: return False
        return True
    
    def list_instances():
        # Lists all valid instance directories inside .game/instances/.
        instances_root = os.path.join(MC_DIR, "instances")
        if not os.path.exists(instances_root): return []
        return sorted([d for d in os.listdir(instances_root) if os.path.isdir(os.path.join(instances_root, d))])
    

    def create_instance_dirs(instance_name):
        # Creates the full instance directory structure.
        inst_root = os.path.join(MC_DIR, "instances", instance_name)
        for d in INSTANCE_DIRS:
            os.makedirs(os.path.join(inst_root, d), exist_ok=True)
        return inst_root

    def get_instance_version(instance_name):
        # Detects the version installed in an instance.
        inst_root = os.path.join(MC_DIR, "instances", instance_name)
        primary_marker = os.path.join(inst_root, ".primary_version")
        if os.path.exists(primary_marker):
            with open(primary_marker, 'r', encoding='utf-8') as f:
                return f.read().strip()
        # Fallback to the old method (first directory)
        versions_dir = os.path.join(inst_root, "versions")
        if not os.path.exists(versions_dir): return None
        entries = [d for d in os.listdir(versions_dir) if os.path.isdir(os.path.join(versions_dir, d))]
        return entries[0] if entries else None

    # ---- PARITY-001: OFFLINE SKIN/CAPE "REBORN" HELPERS ----
    # Offline Skin/Cape constants
    _CSL_SLUG = "customskinloader"   # Modrinth project slug for CustomSkinLoader
    _QFAPI_SLUG = "qfapi"            # Modrinth slug for Quilted Fabric API (SUG-110: verify on project page)
    _PNG_SIG = b"\x89PNG"            # PNG magic bytes for skin/cape validation

    def detect_instance_loader(inst_name):
        # Return the mod-loader kind for an instance, or 'vanilla' if none detected.
        if not inst_name:
            return 'vanilla'
        ver = get_instance_version(inst_name)
        if not ver: return 'vanilla'
        vlow = ver.lower()
        if 'fabric' in vlow: return 'fabric'
        if 'quilt' in vlow: return 'quilt'
        if 'neoforge' in vlow: return 'neoforge'
        if 'forge' in vlow: return 'forge'
        return 'vanilla'

    def get_base_game_version(inst_name, current_ver):
        # Extract the base vanilla version from a modded version ID or its JSON.
        v_dir = os.path.join(MC_DIR, "instances", inst_name, "versions", current_ver)
        v_path = os.path.join(v_dir, f"{current_ver}.json")
        if os.path.exists(v_path):
            try:
                with open(v_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and 'inheritsFrom' in data:
                    return data['inheritsFrom']
            except Exception: pass
        # Fallback: parse from a trailing "x.y.z" segment (e.g. fabric-loader-0.15.0-1.20.1)
        parts = current_ver.split('-')
        if len(parts) >= 3 and '.' in parts[-1]:
            return parts[-1]
        return current_ver

    def _modrinth_pick_jar(slug, loader, game_version):
        # Resolve (url, sha1) for the first .jar of a Modrinth project, or (None, None).
        try:
            api_url = f"https://api.modrinth.com/v2/project/{slug}/version?loaders=[\"{loader}\"]&game_versions=[\"{game_version}\"]"
            versions = net_client.get(api_url, timeout=10).json()
            if not versions:
                api_url = f"https://api.modrinth.com/v2/project/{slug}/version?game_versions=[\"{game_version}\"]"
                versions = net_client.get(api_url, timeout=10).json()
            if versions:
                for _jf in versions[0].get('files', []):
                    if _jf.get('filename', '').endswith('.jar'):
                        return _jf.get('url'), (_jf.get('hashes') or {}).get('sha1')
        except Exception as e:
            logger.error(f"[ ❌ ] Modrinth lookup failed for '{slug}': {e}")
        return None, None

    def _modrinth_first_jar(slug, loader, game_version):
        # SUG-007: URL-only wrapper over _modrinth_pick_jar (which also yields sha1).
        return _modrinth_pick_jar(slug, loader, game_version)[0]

    def reborn_instance_as_modded(inst_name, loader_type):
        # Convert an existing Vanilla instance into a modded one IN PLACE.
        # Never touches saves/config/options/resourcepacks. Snapshots .primary_version
        # first and restores it on any failure (Q5).
        inst_dir = os.path.join(MC_DIR, "instances", inst_name)
        pv_path = os.path.join(inst_dir, ".primary_version")
        old_pv = None
        if os.path.exists(pv_path):
            try:
                with open(pv_path, 'r', encoding='utf-8') as f: old_pv = f.read().strip()
            except Exception: old_pv = None
        mc_version = get_base_game_version(inst_name, get_instance_version(inst_name) or '') or ''
        print(f"[ 🔄 ] \033[1;95mReborn: converting '{inst_name}' to {loader_type.capitalize()}...\033[0m")
        try:
            if loader_type in ('fabric', 'quilt'):
                is_quilt = (loader_type == 'quilt')
                base_url = "https://meta.quiltmc.org/v3" if is_quilt else "https://meta.fabricmc.net/v2"
                name_cap = "Quilt" if is_quilt else "Fabric"
                r = net_client.get(f"{base_url}/versions/loader/{mc_version}")
                loaders = [L['loader']['version'] for L in r.json()]
                if is_quilt:
                    loaders.sort(key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)], reverse=True)
                if not loaders:
                    raise RuntimeError(f"No {name_cap} loaders found for {mc_version}")
                loader_ver = loaders[0]
                loader_id = f"{loader_type}-loader-{loader_ver}-{mc_version}"
                prof_r = net_client.get(f"{base_url}/versions/loader/{mc_version}/{loader_ver}/profile/json")
                prof_json = prof_r.json()
                v_dir = os.path.join(inst_dir, "versions", loader_id)
                os.makedirs(v_dir, exist_ok=True)
                with open(os.path.join(v_dir, f"{loader_id}.json"), 'w', encoding='utf-8') as f:
                    json.dump(prof_json, f, indent=4)
                new_pv = loader_id
            elif loader_type in ('forge', 'neoforge'):
                is_neo = (loader_type == 'neoforge')
                name_cap = "NeoForge" if is_neo else "Forge"
                if is_neo:
                    r = net_client.get("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
                    all_vers = r.json()['versions']
                    prefix = mc_version[2:] + '.'
                    valid_vers = [v for v in all_vers if v.startswith(prefix)]
                    if not valid_vers: raise RuntimeError(f"No {name_cap} versions for {mc_version}")
                    valid_vers.sort(key=lambda x: [int(p) if p.isdigit() else p for p in x.split('.')])
                    loader_ver = valid_vers[-1]
                    installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_ver}/neoforge-{loader_ver}-installer.jar"
                    loader_id = f"neoforge-{loader_ver}"
                else:
                    _forge_promos_url = b64d('aHR0cHM6Ly9maWxlcy5taW5lY3JhZnRmb3JnZS5uZXQvbmV0L21pbmVjcmFmdGZvcmdlL2ZvcmdlL3Byb21vdGlvbnNfc2xpbS5qc29u')
                    _forge_maven_base = b64d('aHR0cHM6Ly9tYXZlbi5taW5lY3JhZnRmb3JnZS5uZXQvbmV0L21pbmVjcmFmdGZvcmdlL2ZvcmdlLw==')
                    r = net_client.get(_forge_promos_url)
                    promos = r.json()['promos']
                    target_key = f"{mc_version}-recommended"
                    if target_key not in promos: target_key = f"{mc_version}-latest"
                    if target_key not in promos: raise RuntimeError(f"No {name_cap} versions for {mc_version}")
                    loader_ver = promos[target_key]
                    installer_url = f"{_forge_maven_base}{mc_version}-{loader_ver}/forge-{mc_version}-{loader_ver}-installer.jar"
                    loader_id = f"forge-{mc_version}-{loader_ver}"
                installer_path = os.path.join(MC_DIR, "cache", f"{loader_id}-installer.jar")
                get(installer_url, installer_path)
                # Spoof the vanilla dir structure the installer expects (never touches saves)
                os.makedirs(os.path.join(inst_dir, "versions"), exist_ok=True)
                os.makedirs(os.path.join(inst_dir, "libraries"), exist_ok=True)
                lp = os.path.join(inst_dir, "launcher_profiles.json")
                if not os.path.exists(lp):
                    with open(lp, 'w') as f: f.write("{}")
                java_bin = get_installer_java(17)
                try:
                    subprocess.run([java_bin, "-jar", installer_path, "--installClient", inst_dir], check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as _cpe:
                    raise RuntimeError(f"installer exited {_cpe.returncode}: {(_cpe.stderr or _cpe.stdout or '').strip()[-800:]}")
                v_dirs = [d for d in os.listdir(os.path.join(inst_dir, "versions")) if os.path.isdir(os.path.join(inst_dir, "versions", d))]
                if loader_id in v_dirs: new_pv = loader_id
                elif v_dirs:
                    v_dirs.sort(key=lambda d: os.path.getmtime(os.path.join(inst_dir, "versions", d)))
                    new_pv = v_dirs[-1]
                else: new_pv = loader_id
            else:
                raise RuntimeError(f"Unsupported reborn loader: {loader_type}")
            with open(pv_path, 'w', encoding='utf-8') as f: f.write(new_pv)
            return True
        except Exception as e:
            logger.error(f"[ ❌ ] Reborn failed: {e}")
            # Q5 rollback: restore the previous .primary_version so the instance still launches
            if old_pv is not None:
                try:
                    with open(pv_path, 'w', encoding='utf-8') as f: f.write(old_pv)
                except Exception: pass
            return False

    def install_customskinloader(inst_dir, loader_type, mc_version, pre_url=None, pre_sha=None):
        # Deploy CustomSkinLoader (+ Fabric API/QFAPI for fabric/quilt) from Modrinth into mods/.
        # Idempotent: skips re-download if the CSL jar is already present. Returns success bool.
        # BUG-117 residual: pre_url/pre_sha thread an already-resolved compat hit through,
        # eliminating the duplicate Modrinth round-trip.
        mods_dir = os.path.join(inst_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)
        csl_jar = os.path.join(mods_dir, "CustomSkinLoader.jar")
        if not os.path.exists(csl_jar):
            print(f"[ 🧵 ] \033[1;94mFetching CustomSkinLoader for {mc_version} ({loader_type})...\033[0m")
            _csl_url, _csl_sha = (pre_url, pre_sha) if pre_url else _modrinth_pick_jar(_CSL_SLUG, loader_type, mc_version)
            if not _csl_url:
                logger.warning("[ ⚠️ ] Could not find a CustomSkinLoader JAR on Modrinth.")
                return False
            try:
                get(_csl_url, csl_jar, expected_hash=_csl_sha)
            except Exception as e:
                logger.error(f"[ ❌ ] Failed to download CustomSkinLoader: {e}")
                return False
        # Q2: auto-deploy Fabric API / Quilted Fabric API alongside CSL
        if loader_type in ('fabric', 'quilt'):
            _api_slug = 'fabric-api' if loader_type == 'fabric' else _QFAPI_SLUG
            _api_marker = 'fabric-api' if loader_type == 'fabric' else 'qfapi'
            if not glob.glob(os.path.join(mods_dir, f"{_api_marker}*.jar")):
                print(f"[ 🧵 ] \033[1;94mFetching Fabric API for {mc_version}...\033[0m")
                _api_url, _api_sha = _modrinth_pick_jar(_api_slug, loader_type, mc_version)
                if _api_url:
                    try:
                        get(_api_url, os.path.join(mods_dir, f"{_api_marker}.jar"), expected_hash=_api_sha)
                    except Exception as e:
                        logger.warning(f"[ ⚠️ ] Fabric API download failed; continuing without it: {e}")
                else:
                    logger.warning("[ ⚠️ ] Fabric API not found; continuing without it")
        return True

    def apply_skin_cape(inst_dir, username, skin_path, cape_path):
        # Copy the PNG(s) into CSL's LocalSkin cache. Purely local; safe offline (Q4).
        csl_root = os.path.join(inst_dir, "CustomSkinLoader")
        skin_dir = os.path.join(csl_root, "Skin")
        cape_dir = os.path.join(csl_root, "Cape")
        os.makedirs(skin_dir, exist_ok=True)
        os.makedirs(cape_dir, exist_ok=True)
        if skin_path and os.path.isfile(skin_path):
            shutil.copy2(skin_path, os.path.join(skin_dir, f"{username}.png"))
            print(f"[ 👕 ] \033[1;92mSkin applied:\033[0m {os.path.join(skin_dir, username + '.png')}")
        if cape_path and os.path.isfile(cape_path):
            shutil.copy2(cape_path, os.path.join(cape_dir, f"{username}.png"))
            print(f"[ 🦸 ] \033[1;92mCape applied:\033[0m {os.path.join(cape_dir, username + '.png')}")
        config_path = os.path.join(csl_root, "CustomSkinLoader.json")
        if not os.path.exists(config_path):
            config = {
                "enableLocalSkinCache": True, "localSkinCache": "Skin",
                "enableLocalCapeCache": True, "localCapeCache": "Cape",
                "enableUpdate": True
            }
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)

    def handle_skin_cape_flags():
        # Offline Skin/Cape (PARITY-001): full reborn-or-apply dispatch.
        # Sterile modes never modify anything.
        if args.dry_run or args.check_java:
            return
        for _flag, _label in ((args.set_skin, 'Skin'), (args.set_cape, 'Cape')):
            if _flag:
                if not os.path.isfile(_flag):
                    print(f"[ ❌ ] \033[1;91m{_label} file not found: {_flag}\033[0m"); sys.exit(EXIT_FILES)
                try:
                    with open(_flag, 'rb') as _pf:
                        if _pf.read(4) != _PNG_SIG:
                            print(f"[ ❌ ] \033[1;91m{_label} file is not a valid PNG: {_flag}\033[0m"); sys.exit(EXIT_FILES)
                except OSError:
                    print(f"[ ❌ ] \033[1;91mCannot read {_label} file: {_flag}\033[0m"); sys.exit(EXIT_FILES)
        loader_kind = detect_instance_loader(INSTANCE_NAME)
        if loader_kind == 'vanilla':
            # Vanilla instance -> needs a mod loader before CSL can work.
            _headless_loader = getattr(args, 'loader', 'vanilla')
            _chosen_loader = None
            if ASSUME_YES:
                # Q3: non-interactive reborn must be explicit; honor --loader if given, else refuse.
                if _headless_loader not in ('fabric', 'quilt', 'forge', 'neoforge'):
                    print("[ ❌ ] \033[1;91m--set-skin/--set-cape on a Vanilla instance requires an explicit --loader <fabric|quilt|forge|neoforge> in non-interactive mode.\033[0m")
                    sys.exit(EXIT_USAGE)
                if args.offline:
                    print("[ ❌ ] \033[1;91mNetwork access is required to install a mod loader and CustomSkinLoader (offline mode active).\033[0m")
                    sys.exit(EXIT_NETWORK)
                _chosen_loader = _headless_loader
            else:
                if args.offline:
                    print("[ ❌ ] \033[1;91mNetwork access is required to install a mod loader and CustomSkinLoader (offline mode active).\033[0m")
                    sys.exit(EXIT_NETWORK)
                print(f"\n[ ⚠️ ] \033[1;93mInstance '{INSTANCE_NAME}' is Vanilla. Skins/capes require a mod loader.\033[0m")
                try:
                    _ans = input("       Install a mod loader on this instance now? [Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n[ 💀 ] Shutdown requested by user. BYE...\n"); sys.exit(0)
                if _ans not in ('', 'y', 'yes'):
                    print("[ ℹ️ ] Reborn declined. No changes were made."); sys.exit(0)
                _loader_opts = [
                    {"id": "fabric",   "display": "\033[1;95mFabric\033[0m (Recommended)", "type": "loader"},
                    {"id": "quilt",    "display": "\033[1;94mQuilt\033[0m",                 "type": "loader"},
                    {"id": "forge",    "display": "\033[1;92mForge\033[0m",                 "type": "loader"},
                    {"id": "neoforge", "display": "\033[1;96mNeoForge\033[0m",              "type": "loader"}
                ]
                # NOTE: interactive_select() is only defined inside the `if not VERSION:` branch,
                # which is skipped for existing instances — use a self-contained numbered menu here.
                print(f"\n\033[1;96m------ Select Mod Loader for Reborn ------\033[0m")
                for _li, _lo in enumerate(_loader_opts):
                    print(f"    \033[1;96m{_li+1}\033[0m. {_lo['display']}")
                while True:
                    try:
                        _lsel = input(f"\n    \033[1;97mSelect Loader\033[0m [1-{len(_loader_opts)}] (blank = cancel): ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print("\n[ 💀 ] Shutdown requested by user. BYE...\n"); sys.exit(0)
                    if not _lsel:
                        print("[ ℹ️ ] Reborn cancelled. No changes were made."); sys.exit(0)
                    try:
                        _lidx = int(_lsel) - 1
                        if 0 <= _lidx < len(_loader_opts):
                            _chosen_loader = _loader_opts[_lidx]['id']; break
                    except ValueError:
                        pass
                    print("[ ❌ ] \033[1;91mInvalid selection.\033[0m")
            if not reborn_instance_as_modded(INSTANCE_NAME, _chosen_loader):
                print("[ ❌ ] \033[1;91mReborn failed. Instance left unchanged.\033[0m")
                sys.exit(1)
            base_ver = get_base_game_version(INSTANCE_NAME, VERSION or '')
            if not install_customskinloader(INST_DIR, _chosen_loader, base_ver):
                print("[ ❌ ] \033[1;91mCustomSkinLoader install failed (incompatible or unavailable).\033[0m")
                sys.exit(1)
        else:
            # Already modded -> ensure CSL is present, then apply.
            base_ver = get_base_game_version(INSTANCE_NAME, VERSION or "")
            _csl_present = bool(glob.glob(os.path.join(INST_DIR, "mods", "CustomSkinLoader*.jar")))
            if not _csl_present:
                if args.offline:
                    # Q4: offline with no CSL yet -> cannot install.
                    print("[ ❌ ] \033[1;91mNetwork access is required to install CustomSkinLoader.\033[0m")
                    sys.exit(EXIT_NETWORK)
                _csl_hit, _csl_sha = _modrinth_pick_jar(_CSL_SLUG, loader_kind, base_ver)
                if _csl_hit is None:
                    print(f"[ ❌ ] \033[1;91mCustomSkinLoader has no build for {loader_kind.capitalize()} on game version {base_ver} — not compatible.\033[0m")
                    sys.exit(EXIT_USAGE)
                if ASSUME_YES:
                    _ans = 'y'
                else:
                    try:
                        _ans = input(f"       CustomSkinLoader is needed for skins/capes. Install it now? [Y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print("\n[ 💀 ] Shutdown requested by user. BYE...\n"); sys.exit(0)
                if _ans not in ('', 'y', 'yes'):
                    print("[ ℹ️ ] CustomSkinLoader installation declined."); sys.exit(0)
                if not install_customskinloader(INST_DIR, loader_kind, base_ver, pre_url=_csl_hit, pre_sha=_csl_sha):
                    print("[ ❌ ] \033[1;91mCustomSkinLoader install failed.\033[0m")
                    sys.exit(1)
            # CSL already present -> purely local re-apply (Q4 offline-safe); skip network entirely.
        # Apply the PNG(s) once a loader + CSL are in place (or CSL already existed).
        apply_skin_cape(INST_DIR, USERNAME, args.set_skin, args.set_cape)
    if getattr(args, 'clone_instance', None): clone_instance(args.clone_instance, getattr(args, 'clone_as', None)); sys.exit(0)
    if getattr(args, 'export_instance', None): export_instance(args.export_instance, args.export_saves, args.export_full); sys.exit(0)
    if getattr(args, 'import_instance', None): import_instance(args.import_instance); sys.exit(0)
    if getattr(args, 'snapshot', None):
        _p = create_snapshot(args.snapshot, 'full')
        print(f"[ ✅ ] Snapshot written: \033[1;92m{_p}\033[0m"); sys.exit(0)
    if getattr(args, 'list_snapshots', None):
        list_snapshots(args.list_snapshots); sys.exit(0)
    if getattr(args, 'restore_snapshot', None):
        # SUG-115: bare --restore-snapshot (const '__latest__') falls back to --instance
        _rs_name = args.restore_snapshot
        if _rs_name == "__latest__":
            _rs_name = getattr(args, 'instance', None)
            if not _rs_name:
                print("[ ❌ ] \033[1;91m--restore-snapshot requires an instance NAME, or combine it with --instance NAME.\033[0m")
                sys.exit(EXIT_USAGE)
        restore_snapshot(_rs_name, getattr(args, 'snapshot_file', None)); sys.exit(0)

    def load_instance_profile(inst_root, profile_name=None):
        profiles_file = os.path.join(inst_root, "profiles.json")
        if not os.path.exists(profiles_file):
            return None, profile_name
            
        try:
            with open(profiles_file, 'r', encoding='utf-8') as f:
                profiles_data = json.load(f)
            profiles = profiles_data.get("profiles", {}) if isinstance(profiles_data, dict) else {}
        except Exception:
            logger.error("profiles.json is corrupt. Ignoring profiles.")
            return None, profile_name
        if not profiles: return None, profile_name
        
        target_profile = profile_name
        if not target_profile:
            if len(profiles) > 1 and not args.offline:
                choices = list(profiles.keys())
                print("\n\033[1;96m  ---- INSTANCE PROFILES ----\033[0m")
                for i, p in enumerate(choices):
                    print(f"    \033[1;96m{i+1}\033[0m. \033[1;97m{p}\033[0m")
                try:
                    sel = input(f"\n    \033[1;97mSelect Profile\033[0m [1-{len(choices)}] (blank = default): ").strip()
                    if sel:
                        idx = int(sel) - 1
                        if 0 <= idx < len(choices):
                            target_profile = choices[idx]
                except ValueError:
                    print("[ ℹ️ ] Invalid selection. Using default profile.")
                except KeyboardInterrupt:
                    print("\n[ 💀 ] Shutdown requested by user. BYE...")
                    sys.exit(0)
            
            if not target_profile:
                target_profile = profiles_data.get("default", list(profiles.keys())[0])
                
        if target_profile not in profiles:
            logger.warning(f"Profile '{target_profile}' not found in profiles.json")
            return None, target_profile
            
        prof = profiles[target_profile]
        print(f"[ 🚀 ] \033[1;92mLoaded Profile:\033[0m \033[1;97m{target_profile}\033[0m")
        return prof, target_profile


    def analyze_crash_logs(inst_dir):
        logs_to_check = []
        
        # Check for hs_err_pid*.log in the instance root
        logs_to_check.extend(glob.glob(os.path.join(inst_dir, "hs_err_pid*.log")))
        
        # Check for crash reports in crash-reports dir
        crash_dir = os.path.join(inst_dir, "crash-reports")
        if os.path.exists(crash_dir):
            logs_to_check.extend(glob.glob(os.path.join(crash_dir, "crash-*.txt")))
            
        if not logs_to_check: return
        
        # Get the most recent log
        latest_log = max(logs_to_check, key=os.path.getmtime)
        
        # Only analyze if it was created in the last 24 hours to avoid stale warnings
        if time.time() - os.path.getmtime(latest_log) > CRASH_STALENESS_S: return
        
        with open(latest_log, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
        diagnostic = None
        suggestion = None
        
        if "OutOfMemoryError" in content:
            diagnostic = "Out of memory"
            suggestion = "Increase RAM with -m flag (e.g. -m 4G)"
        elif "StackOverflowError" in content:
            diagnostic = "Stack overflow"
            suggestion = "Add -Xss4m via --jvm-flags"
        elif "EXCEPTION_ACCESS_VIOLATION" in content:
            diagnostic = "GPU/driver or native access violation"
            suggestion = "Update graphics drivers, disable shaders, or check native mods"
        elif "SIGBUS" in content or "SIGSEGV" in content:
            diagnostic = "Native crash (Segmentation Fault)"
            suggestion = "Update drivers, verify game files with -r"
        elif "MixinApplyError" in content or "mixin" in content.lower():
            diagnostic = "Incompatible mod (Mixin Error)"
            suggestion = "Check mod compatibility for this version"
        elif "ClassNotFoundException" in content:
            diagnostic = "Missing class"
            suggestion = "Verify mods or recheck files with -r"
        elif "UnsupportedClassVersionError" in content:
            diagnostic = "Wrong Java version"
            suggestion = "Update Java or force a specific version with --java"
        elif "GLContext" in content or "WGL" in content or "GLX" in content:
            diagnostic = "Graphics issue (OpenGL)"
            suggestion = "Update drivers, try Mesa on Linux"
        elif "ModResolutionException" in content:
            diagnostic = "Mod dependency conflict"
            suggestion = "Check mod versions and required dependencies"
        elif "hs_err_pid" in latest_log:
            diagnostic = "JVM Hard Crash"
            suggestion = "Review the hs_err_pid log file manually"
            
        if diagnostic:
            print(f"\n[ 🔍 ] \033[1;93mCRASH LOG ANALYZER:\033[0m")
            print(f"       Detected recent crash: \033[1;91m{os.path.basename(latest_log)}\033[0m")
            print(f"       \033[1;97mDiagnostic:\033[0m \033[1;91m{diagnostic}\033[0m")
            print(f"       \033[1;97mSuggested Fix:\033[0m \033[1;92m{suggestion}\033[0m\n")

    # UTILITIES
    import logging
    from logging.handlers import RotatingFileHandler
    
    class ANSIFormatter(logging.Formatter):
        FORMATS = {
            logging.DEBUG: "\033[1;90m[ 🐛 ] DEBUG: %(message)s\033[0m",
            logging.INFO: "%(message)s",
            logging.WARNING: "[ ⚠️ ] \033[1;93mWARNING: %(message)s\033[0m",
            logging.ERROR: "[ ❌ ] \033[1;91mERROR: %(message)s\033[0m",
            logging.CRITICAL: "[ 💀 ] \033[1;41mCRITICAL: %(message)s\033[0m"
        }
        # SMELL-111: prebuilt formatters instead of one-per-record construction
        _PREBUILT = {level: logging.Formatter(fmt) for level, fmt in FORMATS.items()}
        _DEFAULT = logging.Formatter("%(message)s")

        def format(self, record):
            return self._PREBUILT.get(record.levelno, ANSIFormatter._DEFAULT).format(record)

    def setup_logger():
        log = logging.getLogger("NuxCraft")
        log.setLevel(logging.DEBUG)
        if log.hasHandlers(): log.handlers.clear()
        
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(ANSIFormatter())
        log.addHandler(ch)
        
        global_log_dir = os.path.join(MC_DIR, "logs")
        if STERILE:
            # SUG-013: sterile modes never write launcher.log to disk
            return log
        os.makedirs(global_log_dir, exist_ok=True)
        global_fh = RotatingFileHandler(os.path.join(global_log_dir, "launcher.log"), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
        global_fh.setLevel(logging.DEBUG)
        global_fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        log.addHandler(global_fh)
        
        return log

    logger = setup_logger()
    
    def add_instance_log(inst_name):
        if STERILE:
            # SUG-013: sterile modes never write per-instance launcher.log
            return
        log_dir = os.path.join(MC_DIR, "instances", inst_name, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.abspath(os.path.join(log_dir, "launcher.log"))
        if any(getattr(h, 'baseFilename', None) == log_path for h in logger.handlers):
            return
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)


    class NetworkClient:
        def __init__(self):
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": f"NuxCraft-PyCher/{launcher_version} ({platform_os})"})
            
        def request(self, method, url, retries=3, **kwargs):
            for attempt in range(retries):
                try:
                    kwargs.setdefault('timeout', 15)
                    r = self.session.request(method, url, **kwargs)
                    r.raise_for_status()
                    return r
                except requests.exceptions.RequestException as e:
                    _resp = getattr(e, 'response', None)
                    if _resp is not None:
                        if _resp.status_code in (400, 401, 403, 404, 405, 410, 451):
                            raise
                    if attempt == retries - 1:
                        logger.debug(f"Network request failed after {retries} attempts: {url} -> {e}")
                        raise
                    logger.debug(f"Retrying network request ({attempt+1}/{retries}): {url}")
                    time.sleep(min(8, 2 ** attempt))

        def get(self, url, **kwargs): return self.request("GET", url, **kwargs)

    net_client = NetworkClient()
    
    def get_adoptium_java(major_version):
        if args.dry_run: return "java"
        # Do NOT reference is_freebsd here: it is only defined in the POSIX branch (NameError on Windows)
        if platform.system().lower().startswith("freebsd"):
            logger.info("[ ℹ️ ] Adoptium does not provide FreeBSD builds. Using system Java (linuxator).")
            return "java"
        # Downloads and extracts the required Java version from Adoptium API into .game/runtimes/.
        os_map = {"windows": "windows", "darwin": "mac", "linux": "linux"}
        arch_map = {
            "amd64": "x64", "x86_64": "x64",
            "x86": "x86", "i386": "x86", "i686": "x86",
            "arm64": "aarch64", "aarch64": "aarch64", "arm64e": "aarch64",
            "armv7l": "arm", "arm": "arm",
            "riscv64": "riscv64", "ppc64le": "ppc64le", "s390x": "s390x"
        }
        ad_os = os_map.get(platform.system().lower(), "linux")
        ad_arch = arch_map.get(platform.machine().lower(), "x64")
        
        api_url = f"https://api.adoptium.net/v3/assets/latest/{major_version}/hotspot?os={ad_os}&architecture={ad_arch}&image_type=jre"
        try:
            r = net_client.get(api_url, timeout=10)
            data = r.json()
            if not data: return "java"
            pkg = data[0]['binary']['package']
            dl_url, pkg_name = pkg['link'], pkg['name']
            expected_checksum = pkg.get('checksum')
            
            runtime_dir = os.path.join(MC_DIR, "runtimes", f"{ad_os}-{ad_arch}", f"java-{major_version}")
            os.makedirs(runtime_dir, exist_ok=True)
            
            # Find the actual java executable inside if already extracted
            exe_ext = ".exe" if ad_os == "windows" else ""
            incomplete_marker = os.path.join(runtime_dir, ".extraction_incomplete")
            if os.path.exists(incomplete_marker):
                logger.warning("[ ⚠️ ] Previous Java extraction was interrupted. Cleaning up and re-extracting...")
                shutil.rmtree(runtime_dir, ignore_errors=True)
                os.makedirs(runtime_dir, exist_ok=True)
            for root, dirs, files in os.walk(runtime_dir):
                if f"java{exe_ext}" in files:
                    return os.path.join(root, f"java{exe_ext}")
            
            pkg_path = os.path.join(runtime_dir, pkg_name)
            if not os.path.exists(pkg_path):
                print(f"[ ☕ ] \033[1;94mDownloading Java {major_version} ({ad_os}-{ad_arch})...\033[0m")
                get(dl_url, pkg_path, expected_hash=expected_checksum, hash_algo=hashlib.sha256)
            elif expected_checksum:
                hasher = hashlib.sha256()
                with open(pkg_path, 'rb') as f:
                    while chunk := f.read(IO_CHUNK): hasher.update(chunk)
                if hasher.hexdigest().lower() != expected_checksum.lower():
                    logger.warning("[ ⚠️ ] Adoptium package checksum mismatch. Re-downloading...")
                    os.remove(pkg_path)
                    get(dl_url, pkg_path, expected_hash=expected_checksum, hash_algo=hashlib.sha256)
            
            if not os.path.exists(pkg_path):
                print(f"[ ! ] \033[1;93mWarning: Could not download Java (Offline Mode). Falling back to system java.\033[0m")
                return "java"
                
            try:
                print(f"[ 📦 ] \033[1;94mExtracting Java Runtime...\033[0m")
                _rt_abs = os.path.abspath(runtime_dir)
                with open(incomplete_marker, 'w') as _mf: _mf.write('')
                if pkg_name.endswith('.zip'):
                    with zipfile.ZipFile(pkg_path, 'r') as z:
                        for _member in z.namelist():
                            _member_path = os.path.abspath(os.path.join(runtime_dir, _member))
                            if not (_member_path == _rt_abs or _member_path.startswith(_rt_abs + os.sep)):
                                raise Exception(f"Attempted path traversal in zip file: {_member}")
                        z.extractall(runtime_dir)
                elif pkg_name.endswith('.tar.gz'):
                    with tarfile.open(pkg_path, 'r:gz') as t:
                        for member in t.getmembers():
                            member_path = os.path.abspath(os.path.join(runtime_dir, member.name))
                            if not (member_path == _rt_abs or member_path.startswith(_rt_abs + os.sep)):
                                raise Exception(f"Attempted path traversal in tar file: {member.name}")
                        # PEP 706 'data' filter strips symlinks/hardlinks/special modes where supported
                        try:
                            t.extractall(runtime_dir, filter='data')
                        except TypeError:
                            t.extractall(runtime_dir)
                if os.path.exists(pkg_path): os.remove(pkg_path)
                if os.path.exists(incomplete_marker): os.remove(incomplete_marker)
            except Exception as extract_err:
                logger.error(f"[ ❌ ] Failed to extract Java runtime: {extract_err}")
                shutil.rmtree(runtime_dir, ignore_errors=True)
                return "java"
            
            for root, dirs, files in os.walk(runtime_dir):
                if f"java{exe_ext}" in files:
                    java_bin = os.path.join(root, f"java{exe_ext}")
                    if ad_os != "windows": os.chmod(java_bin, 0o755)
                    return java_bin
            return "java"
        except Exception as e:
            print(f"[ ! ] \033[1;91mFailed to auto-download Java: {e}\033[0m")
            return "java"

    def get_installer_java(major_version=17):
        if shutil.which("java"):
            try:
                _v = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=10)
                _m = re.search(r'version "(\d+)', (_v.stderr or "") + (_v.stdout or ""))
                if _m and int(_m.group(1)) >= major_version:
                    return "java"
                logger.info(f"[ ℹ️ ] System Java too old for installers (need {major_version}+). Fetching a runtime...")
            except Exception:
                pass
        return get_adoptium_java(major_version)
    
    # ---- PHASE 2: JAVA AUTO-DETECTION ----
    def _java_major_from_path(p):
        # Parse a major version from an install path (jdk-17.0.2, openjdk-21, jre1.8.0_392, java-11-openjdk)
        _m = re.search(r'(?:jdk|jre|java|openjdk|adoptium|zulu|temurin|liberica)[-_]?(\d{1,2})|v?(\d{1,2})\.\d', os.sep.join(reversed(p.split(os.sep)[-4:])), re.IGNORECASE)
        if _m:
            _major = int(_m.group(1) or _m.group(2))
            # Legacy 1.x layout (jre1.8.0_392 / jdk1.8): the real major is the minor component
            if _major == 1:
                _m18 = re.search(r'(?<!\d)1\.(\d{1,2})', p)
                if _m18: return int(_m18.group(1))
            return _major
        _m2 = re.search(r'(\d{1,2})\.\d+\.\d+', p)  # bare 17.0.2 style
        return int(_m2.group(1)) if _m2 else None
    
    def detect_java_runtimes():
        # Q7: PATH + Windows dirs + Linux dirs + macOS JVMs + FreeBSD + managed runtimes. Zero dependencies.
        cands = []
        def _add(binpath, source):
            if binpath and os.path.isfile(binpath) and binpath not in [c['path'] for c in cands]:
                cands.append({'path': binpath, 'major': _java_major_from_path(binpath), 'source': source})
        for _n in ("java", "javaw"):
            _w = shutil.which(_n)
            if _w: _add(_w, "PATH")
        if is_windows:
            for _root in (r"C:\Program Files", r"C:\Program Files (x86)"):
                if os.path.isdir(_root):
                    for _sub in os.listdir(_root):
                        if re.search(r'java|jdk|jre|adoptium|zulu|temurin|liberica|micro', _sub, re.IGNORECASE):
                            _add(os.path.join(_root, _sub, "bin", "java.exe"), "windows")
                            # Vendor dirs (Java/, Eclipse Adoptium/, Zulu/) nest the actual JDK one level deeper
                            _subp = os.path.join(_root, _sub)
                            if os.path.isdir(_subp):
                                try:
                                    for _sub2 in os.listdir(_subp):
                                        _add(os.path.join(_subp, _sub2, "bin", "java.exe"), "windows")
                                except OSError: pass
        else:
            if platform.system().lower() == "linux":
                if os.path.isdir("/usr/lib/jvm"):
                    for _sub in os.listdir("/usr/lib/jvm"):
                        _add(os.path.join("/usr/lib/jvm", _sub, "bin", "java"), "linux")
            elif is_mac:
                _jh = "/Library/Java/JavaVirtualMachines"
                if os.path.isdir(_jh):
                    for _sub in os.listdir(_jh):
                        _add(os.path.join(_jh, _sub, "Contents", "Home", "bin", "java"), "macos")
            elif platform.system().lower().startswith("freebsd"):
                for _d in glob.glob("/usr/local/openjdk*/bin/java"): _add(_d, "freebsd")
        # Managed runtimes under .game/runtimes (any OS-arch dir)
        _rt_root = os.path.join(MC_DIR, "runtimes")
        if os.path.isdir(_rt_root):
            for _root, _dirs, _files in os.walk(_rt_root):
                _exe = "java.exe" if is_windows else "java"
                if _exe in _files:
                    _add(os.path.join(_root, _exe), "managed"); _dirs.clear()
        # Q8: probe -version ONLY where the path didn't yield a major (ambiguity finalists)
        for _c in cands:
            if _c['major'] is None:
                try:
                    _pv = subprocess.run([_c['path'], "-version"], capture_output=True, text=True, timeout=10)
                    _pm = re.search(r'version "(\d+)(?:\.(\d+))?', (_pv.stderr or "") + (_pv.stdout or ""))
                    if _pm:
                        # Legacy "1.8.0_x" output: the real major is the minor component
                        _c['major'] = int(_pm.group(2)) if _pm.group(1) == '1' and _pm.group(2) else int(_pm.group(1))
                except Exception: pass
        return [c for c in cands if c['major'] is not None]
    
    def choose_java_runtime(required_major, runtimes):
        # Prefer exact major; then smallest major >= required; otherwise fall through to Adoptium
        # provisioning (AGENTS.md chain: profile java -> matching local runtime -> Adoptium -> java).
        # Too-old runtimes are never auto-selected: they guarantee an UnsupportedClassVersionError.
        _exact = [c for c in runtimes if c['major'] == required_major]
        _newer = sorted([c for c in runtimes if c['major'] > required_major], key=lambda c: c['major'])
        _pool = _exact or _newer
        if not _pool: return None
        if not _exact and _newer:
            logger.info(f"[ ℹ️ ] No Java {required_major} found locally; a newer major will be offered.")
        if len(_pool) == 1 or ASSUME_YES:
            _pick = max(_pool, key=lambda c: (c['major'] == required_major, c['major']))
            print(f"[ ☕ ] Auto-selected Java \033[1;92m{_pick['major']}\033[0m @ \033[1;97m{_pick['path']}\033[0m ({_pick['source']})")
            return _pick['path']
        # Q11-C: always show the picker when multiple candidates (interactive only)
        print(f"\n\033[1;96m  ---- DETECTED JAVA RUNTIMES (need {required_major}+) ----\033[0m")
        for _i, _c in enumerate(_pool):
            _mark = " \033[1;92m<-- exact match\033[0m" if _c['major'] == required_major else ""
            print(f"    \033[1;96m{_i+1}\033[0m. \033[1;97mJava {_c['major']}\033[0m  {_c['path']}  ({_c['source']}){_mark}")
        while True:
            try:
                _sel = input(f"\n    \033[1;97mSelect Java\033[0m [1-{len(_pool)}]: ").strip()
                _idx = int(_sel) - 1
                if 0 <= _idx < len(_pool): return _pool[_idx]['path']
            except ValueError: pass
            except KeyboardInterrupt:
                print("\n[ 💀 ] Shutdown requested by user. BYE..."); sys.exit(0)
            print("[ ❌ ] \033[1;91mInvalid selection.\033[0m")
    
    INSTANCE_JAVA = None  # set from profiles.json (CB-2.2)
    
    def resolve_final_java(required_major):
        # Q10-A chain. --java PATH and --system-java keep their exact existing semantics.
        if args.java != "java":
            if not os.path.isfile(args.java):
                logger.warning(f"[ ⚠️ ] --java path not found: {args.java}")
            return args.java
        if args.system_java:
            _w = shutil.which("java")
            return _w or "java"
        if INSTANCE_JAVA:
            if os.path.isfile(INSTANCE_JAVA):
                _mj = _java_major_from_path(INSTANCE_JAVA)
                if _mj is None or _mj >= required_major:
                    print(f"[ ☕ ] Using instance Java: \033[1;97m{INSTANCE_JAVA}\033[0m")
                    return INSTANCE_JAVA
                logger.warning(f"[ ⚠️ ] Instance Java {_mj} < required {required_major}; falling through.")
            else:
                logger.warning(f"[ ⚠️ ] Instance Java path missing: {INSTANCE_JAVA}; falling through.")
        _rts = detect_java_runtimes()
        _pick = choose_java_runtime(required_major, _rts)
        if _pick: return _pick
        return get_adoptium_java(required_major)
    
    def get(url, path, expected_hash=None, silent=False, hash_algo=HASH_ALGO):
        if args.dry_run: return
        if args.offline:
            if not os.path.exists(path):
                raise RuntimeError(f"Offline mode: required file missing: {path}")
            return
            
        def verify():
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return False
            if not expected_hash:
                # No hash available: at least validate archive magic bytes
                if path.endswith(('.jar', '.zip')):
                    try:
                        with open(path, 'rb') as f: return f.read(2) == b'PK'
                    except OSError: return False
                if path.endswith(('.tar.gz', '.tgz')):
                    try:
                        with open(path, 'rb') as f: return f.read(2) == b'\x1f\x8b'
                    except OSError: return False
                return True
            hasher = hash_algo()
            with open(path, 'rb') as f:
                while chunk := f.read(IO_CHUNK): hasher.update(chunk)
            return hasher.hexdigest().lower() == expected_hash.lower()

        if verify(): return
        safe_makedirs(path, is_file=True)
        
        is_large_file = path.endswith(('.jar', '.zip', '.tar.gz'))
        existing_size = os.path.getsize(path) if (is_large_file and os.path.exists(path)) else 0
        if existing_size > 0 and not expected_hash:
            # Cannot validate a hashless partial prefix; restart from scratch
            try: os.remove(path)
            except OSError: pass
            existing_size = 0
        headers = {}
        if existing_size > 0:
            headers['Range'] = f'bytes={existing_size}-'
            
        try:
            r = net_client.get(url, stream=True, headers=headers)
            mode = 'wb'
            if r.status_code == 206:
                mode = 'ab'
                content_len = int(r.headers.get('content-length', 0))
                total = existing_size + content_len
            else:
                existing_size = 0
                total = int(r.headers.get('content-length', 0))
                
            with open(path, mode) as f, tqdm(
                total=total, initial=existing_size, unit='B', unit_scale=True, 
                unit_divisor=1024, desc=f"  [ ☕ ] \033[1;94mSyncing {os.path.basename(path)}\033[0m", 
                disable=silent, bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}\033[0m \033[1;97m[{rate_fmt}]\033[0m  "
            ) as bar:
                for chunk in r.iter_content(chunk_size=IO_CHUNK):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))
                # SUG-007: validate bytes written against Content-Length
                _expected = total - existing_size if mode == 'ab' else total
                _written = os.path.getsize(path) - existing_size
                if _expected and _written != _expected:
                    raise ValueError(f"Incomplete download: wrote {_written} of {_expected} bytes for {os.path.basename(path)}")
        except requests.exceptions.RequestException as e:
            if not silent: logger.error(f"Error downloading file: {e.__class__.__name__}")
            raise
            
        if not verify():
            os.remove(path)
            raise ValueError(f"Integrity check failed after downloading {os.path.basename(path)}")
    
    def is_allowed(rules):
        if not rules: return True
        allowed = False if rules[0].get('action') == 'allow' else True
        for r in rules:
            if 'features' in r:
                # Feature-gated args (demo/resolution) are not supported; never match them
                match = False
            elif 'os' in r:
                os_name = r.get('os', {}).get('name')
                if platform_os == "windows":
                    # Old Game versions sometimes use 'win', new ones use 'windows'
                    match = (os_name == "windows" or os_name == "win")
                else:
                    match = (os_name == platform_os)
            else:
                match = True
            
            if match: 
                allowed = (r['action'] == 'allow')
        return allowed

    # INSTANCE & VERSION SELECTION
    last_inst_file = os.path.join(MC_DIR, "cache/last_instance.txt")
    manifest_cache = os.path.join(MC_DIR, "cache/manifest.json")
    INSTANCE_NAME = None
    VERSION, V_URL = None, None
    selected_instance = None  # SMELL-113: always-defined sentinel (TUI / headless-create paths reassign it)
    
    # --last / --offline: Launch last played instance instantly
    if args.offline and os.path.exists(last_inst_file):
        with open(last_inst_file, 'r', encoding='utf-8') as f: INSTANCE_NAME = f.read().strip()
        detected_ver = get_instance_version(INSTANCE_NAME)
        if detected_ver and INSTANCE_NAME:
            VERSION = detected_ver
            print(f"[ ✅ ] Local Authentication Active: Loading instance '\033[1;92m{INSTANCE_NAME}\033[0m' ({VERSION})")
        else:
            print(f"[ ❌ ] \033[1;91mLast instance '{INSTANCE_NAME}' not found or corrupted.\033[0m")
            INSTANCE_NAME = None
    
    # ---- PHASE 1: NON-INTERACTIVE DISPATCH ----
    def _list_versions_cli():
        # Self-contained version lister: cache-first, network fallback, sterile exit.
        _m = None
        try:
            if os.path.exists(manifest_cache) and not args.refresh:
                with open(manifest_cache, 'r', encoding='utf-8') as f: _m = json.load(f)
        except Exception:
            _m = None
        if _m is None:
            # SUG-012: under --offline never touch the network — cache-only
            if args.offline:
                print("[ ❌ ] \033[1;91mOffline mode: no cached version manifest. Run once online (or with --refresh) to populate it.\033[0m")
                sys.exit(EXIT_FILES)
            _src1 = globals().get('manifest_json_remote_source1') or b64d('aHR0cHM6Ly9sYXVuY2hlcm1ldGEubW9qYW5nLmNvbS9tYy9nYW1lL3ZlcnNpb25fbWFuaWZlc3QuanNvbg==')
            _src2 = globals().get('manifest_json_remote_source2') or b64d('aHR0cHM6Ly9waXN0b24tbWV0YS5tb2phbmcuY29tL21jL2dhbWUvdmVyc2lvbl9tYW5pZmVzdC5qc29u')
            for _u in (_src1, _src2):
                try:
                    _m = net_client.get(_u).json(); break
                except Exception: continue
        if not isinstance(_m, dict) or not isinstance(_m.get('versions'), list):
            print(f"[ ❌ ] \033[1;91mNo version manifest available (offline and no cache).\033[0m"); sys.exit(EXIT_FILES)
        for _v in _m['versions']:
            _t = _v.get('type', 'release')
            if args.snapshots and _t != 'snapshot': continue
            if args.beta and _t not in ('old_beta', 'old_alpha'): continue
            if not args.snapshots and not args.beta and _t != 'release': continue
            print(_v.get('id', ''))
        sys.exit(0)
    
    if args.list_instances_flag:
        for _i in list_instances():
            print(f"{_i}\t{get_instance_version(_i) or 'empty'}")
        sys.exit(0)
    if args.list_versions_flag: _list_versions_cli()

    if getattr(args, 'selftest', None):
        # SUG-010: offline self-test of pure launcher internals (no network, no game-dir writes)
        import tempfile
        _failures = []
        def _st(name, cond):
            print(f"    [{'PASS' if cond else 'FAIL'}] {name}")
            if not cond: _failures.append(name)
        print(f"\n\033[1;96m  ---- NuxCraft-PyCher Self-Test ({launcher_version}) ----\033[0m")
        _st("instance name rejects reserved CON", not is_valid_instance_name("CON"))
        _st("instance name rejects traversal '..'", not is_valid_instance_name("..\\x"))
        _st("instance name rejects slash", not is_valid_instance_name("a/b"))
        _st("instance name accepts normal", is_valid_instance_name("My Instance 1"))
        _st("reserved list shared (SMELL-101)", RESERVED_NAMES[0] == "CON" and len(RESERVED_NAMES) == 22)
        _st("java major from deep path", _java_major_from_path("/usr/lib/jvm/java-17-openjdk/bin/java") == 17)
        _st("java major legacy 1.x path", _java_major_from_path("C:\\Java\\jre1.8.0_392\\bin\\java.exe") == 8)
        _st("base version from modded id", get_base_game_version("x", "fabric-loader-0.15.0-1.20.1") == "1.20.1")
        with tempfile.TemporaryDirectory() as _td:
            _t = os.path.join(_td, "a.bin")
            with open(_t, "wb") as f: f.write(b"A" * 4096)
            _t2 = os.path.join(_td, "b.bin")
            with open(_t2, "wb") as f: f.write(b"A" * 4096)
            _st("files_identical true", files_identical(_t, _t2))
            with open(_t2, "ab") as f: f.write(b"B")
            _st("files_identical false on change", not files_identical(_t, _t2))
            reflink_or_copy(_t, os.path.join(_td, "c.bin"))
            _st("reflink_or_copy produces file", os.path.isfile(os.path.join(_td, "c.bin")))
        if _failures:
            print(f"\n[ ❌ ] Self-test failed: {', '.join(_failures)}")
            sys.exit(1)
        print("\n[ ✅ ] Self-test passed.")
        sys.exit(0)
    
    if getattr(args, 'instance', None):
        if args.instance not in list_instances():
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{args.instance}' does not exist.")
            sys.exit(EXIT_INSTANCE)
        INSTANCE_NAME = args.instance
        _iv = get_instance_version(INSTANCE_NAME)
        if not _iv:
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{INSTANCE_NAME}' has no version installed.")
            sys.exit(EXIT_INSTANCE)
        VERSION = _iv
        print(f"[ ✅ ] Non-interactive mode: selected instance '\033[1;92m{INSTANCE_NAME}\033[0m' ({VERSION})")
    
    HEADLESS_CREATE = None
    if getattr(args, 'create_instance', None):
        if not args.mc_version:
            print(f"[ ❌ ] \033[1;91m--create-instance requires --mc-version.\033[0m"); sys.exit(EXIT_USAGE)
        if not is_valid_instance_name(args.create_instance):
            print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid instance name: '{args.create_instance}'."); sys.exit(EXIT_USAGE)
        if args.create_instance in list_instances():
            print(f"[ ❌ ] \033[1;91mError:\033[0m Instance '{args.create_instance}' already exists."); sys.exit(EXIT_INSTANCE)
        HEADLESS_CREATE = {'name': args.create_instance, 'loader': args.loader, 'version': args.mc_version}
        _type_map = {'vanilla': 'create', 'fabric': 'create_fabric', 'quilt': 'create_quilt', 'forge': 'create_forge', 'neoforge': 'create_neoforge'}
        selected_instance = {'type': _type_map[args.loader]}
    
    if not INTERACTIVE and not INSTANCE_NAME and not HEADLESS_CREATE:
        print(f"[ ❌ ] \033[1;91mNon-interactive session with no selection: pass --instance NAME, --create-instance, --last, or a list flag.\033[0m")
        sys.exit(EXIT_USAGE)
    
    if not INSTANCE_NAME and not HEADLESS_CREATE:
        selected_instance = None
        # INSTANCE SELECTION TUI
        def interactive_instance_select(instances, last_instance="", mode="select"):

            if sys.stdout.isatty():
                options = []
                for inst in instances:
                    ver = get_instance_version(inst) or "empty"
                    options.append({"id": inst, "display": f"{inst} ({ver})", "type": "instance"})
                
                if mode == "select":
                    options.append({"id": "__create_new__", "display": "\033[1;93m+ Create New Vanilla Instance\033[0m", "type": "create"})
                    options.append({"id": "__create_fabric__", "display": "\033[1;95m+ Create Fabric Instance\033[0m", "type": "create_fabric"})
                    options.append({"id": "__create_quilt__", "display": "\033[1;94m+ Create Quilt Instance\033[0m", "type": "create_quilt"})
                    options.append({"id": "__create_forge__", "display": "\033[1;92m+ Create Forge Instance\033[0m", "type": "create_forge"})
                    options.append({"id": "__create_neoforge__", "display": "\033[1;96m+ Create NeoForge Instance\033[0m", "type": "create_neoforge"})
                    if instances:
                        options.append({"id": "__delete_instance__", "display": "\033[1;91m- Delete Instance\033[0m", "type": "delete_instance"})
                elif mode == "delete":
                    options.append({"id": "__cancel__", "display": "\033[1;91mCancel\033[0m", "type": "cancel"})
                
                total = len(options)
                curr = 0
                
                if last_instance:
                    for i, opt in enumerate(options):
                        if opt['id'] == last_instance:
                            curr = i
                            break
                
                while True:
                    try:
                        term_height = os.get_terminal_size().lines
                        window_size = max(5, term_height - 8)
                    except Exception:
                        window_size = 15
                    
                    if mode == "select":
                        if ansi_clear:
                            buf = ["\033[H\033[J\n\033[1;96m------ Select Instance ------\033[0m\n"]
                        else:
                            os.system('cls' if is_windows else 'clear')
                            buf = ["\n\033[1;96m------ Select Instance ------\033[0m\n"]
                    else:
                        if ansi_clear:
                            buf = ["\033[H\033[J\n\033[1;91m------ Select Instance to Delete ------\033[0m\n"]
                        else:
                            os.system('cls' if is_windows else 'clear')
                            buf = ["\n\033[1;91m------ Select Instance to Delete ------\033[0m\n"]
                    
                    less_hint = 'Use less / ' if shutil.which("less") else ''
                    buf.append(f"\033[1;97mNavigate: \033[1;96mArrows\033[1;97m ( \033[1;96m↑\033[1;97m and \033[1;96m↓\033[1;97m ) | \033[1;97mSelect: \033[1;96mEnter\033[1;97m | \033[1;97mBack: \033[1;96mEsc\033[1;97m | \033[1;97m{less_hint}Print Mode: \033[1;96mQ\033[1;97m\n\n")
                    
                    start = max(0, min(curr - window_size // 2, total - window_size))
                    end = min(start + window_size, total)
                    
                    for i in range(start, end):
                        opt = options[i]
                        is_selected = (i == curr)
                        is_last = (opt['id'] == last_instance)
                        
                        sel_marker = "  [ \033[1;96mX\033[0m ]\033[1;96m " if is_selected else "  [   ]\033[1;97m "
                        
                        line = f"{sel_marker}{opt['display']}\033[0m"
                        if is_last and mode == "select":
                            line += "  \033[1;91m<-- (Last Played)\033[0m"
                            
                        buf.append(line + "\n")
                        
                    buf.append(f"\n  [ \033[1;94m{curr + 1}\033[0m / \033[1;94m{total}\033[0m ] | Page: \033[1;94m{start+1}\033[0m-\033[1;94m{end}\033[0m\n")
                    
                    sys.stdout.write("".join(buf))
                    sys.stdout.flush()
                    
                    if is_windows:
                        key = msvcrt.getch()
                        if key in (b'\xe0', b'\x00'):
                            key = msvcrt.getch()
                            if key == b'H': curr = max(0, curr - 1)
                            elif key == b'P': curr = min(total - 1, curr + 1)
                        elif key in (b'\r', b'\n'):
                            return options[curr]
                        elif key in (b'\x1b', b'\x03'):
                            if key == b'\x1b' and mode == "delete":
                                return {"id": "__cancel__", "type": "cancel"}
                            print("\n[ 💀 ] Shutdown requested by user. BYE...")
                            sys.exit(0)
                        elif key.lower() == b'q':
                            os.system('cls')
                            return None
                    else:
                        key = _read_posix_key()
                        if key == '\x1b[A': curr = max(0, curr - 1)
                        elif key == '\x1b[B': curr = min(total - 1, curr + 1)
                        elif key in ('\r', '\n'): return options[curr]
                        elif key in ('\x1b', '\x03'):
                            if key == '\x1b' and mode == "delete":
                                return {"id": "__cancel__", "type": "cancel"}
                            print("\n[ 💀 ] Shutdown requested by user. BYE...")
                            sys.exit(0)
                        elif key.lower() == 'q':
                            print("\033[H\033[J", end="")
                            return None
            return None

        while True:
            existing_instances = list_instances()
            last_saved_instance = ""
            if not args.check_java and os.path.exists(last_inst_file):
                with open(last_inst_file, 'r', encoding='utf-8') as f: last_saved_instance = f.read().strip()
            
            selected_instance = interactive_instance_select(existing_instances, last_saved_instance, mode="select")
            
            if selected_instance is None:
                # FALLBACK for Instance Selection
                while True:
                    menu_lines = ["\n\033[1;96m  ---- INSTANCE LIST ----\033[0m"]
                    for i, inst in enumerate(existing_instances):
                        ver = get_instance_version(inst) or "empty"
                        marker = " \033[1;91m<-- [LAST PLAYED]\033[0m" if inst == last_saved_instance else ""
                        menu_lines.append(f"    \033[1;96m{i+1}\033[0m. \033[1;97m{inst}\033[0m ({ver}){marker}")
                    menu_lines.append(f"    \033[1;96m{len(existing_instances)+1}\033[0m. \033[1;93m+ Create New Vanilla Instance\033[0m")
                    menu_lines.append(f"    \033[1;96m{len(existing_instances)+2}\033[0m. \033[1;95m+ Create Fabric Instance\033[0m")
                    menu_lines.append(f"    \033[1;96m{len(existing_instances)+3}\033[0m. \033[1;94m+ Create Quilt Instance\033[0m")
                    menu_lines.append(f"    \033[1;96m{len(existing_instances)+4}\033[0m. \033[1;92m+ Create Forge Instance\033[0m")
                    menu_lines.append(f"    \033[1;96m{len(existing_instances)+5}\033[0m. \033[1;96m+ Create NeoForge Instance\033[0m")
                    if existing_instances:
                        menu_lines.append(f"    \033[1;96m{len(existing_instances)+6}\033[0m. \033[1;91m- Delete Instance\033[0m")
                    
                    menu = "\n".join(menu_lines)
                    display_paged_menu(menu)
                    
                    default_sel = last_saved_instance if last_saved_instance in existing_instances else (existing_instances[0] if existing_instances else "+ Create New Instance")
                    sel = input(f"\n    \033[1;97mSelect Instance\033[0m{f' [ Default: {default_sel} ]' if default_sel else ''}:\033[0m ").strip()
                    
                    if not sel and default_sel:
                        if default_sel == "+ Create New Instance":
                            selected_instance = {'type': 'create'}
                            break
                        else:
                            selected_instance = {'id': default_sel, 'type': 'instance'}
                            break
                    try:
                        idx = int(sel) - 1
                        if 0 <= idx < len(existing_instances):
                            selected_instance = {'id': existing_instances[idx], 'type': 'instance'}
                            break
                        elif idx == len(existing_instances):
                            selected_instance = {'type': 'create'}
                            break
                        elif idx == len(existing_instances) + 1:
                            selected_instance = {'type': 'create_fabric'}
                            break
                        elif idx == len(existing_instances) + 2:
                            selected_instance = {'type': 'create_quilt'}
                            break
                        elif idx == len(existing_instances) + 3:
                            selected_instance = {'type': 'create_forge'}
                            break
                        elif idx == len(existing_instances) + 4:
                            selected_instance = {'type': 'create_neoforge'}
                            break
                        elif existing_instances and idx == len(existing_instances) + 5:
                            selected_instance = {'type': 'delete_instance'}
                            break
                    except ValueError:
                        pass
                    print("[ ❌ ] \033[1;91mInvalid selection.\033[0m")
            
            # Handle instance deletion
            if selected_instance.get('type') == 'delete_instance':
                del_target = None
                if not existing_instances:
                    print("[ ❌ ] \033[1;91mNo instances exist to delete.\033[0m")
                    sys.exit(1)
                    
                del_selected = interactive_instance_select(existing_instances, last_saved_instance, mode="delete")
                
                if del_selected is None:
                    # FALLBACK for Deletion Selection
                    while True:
                        menu_lines = ["\n\033[1;91m---- DELETE INSTANCE ----\033[0m"]
                        for i, inst in enumerate(existing_instances):
                            menu_lines.append(f"  {i+1}. {inst}")
                        menu_lines.append(f"  {len(existing_instances)+1}. \033[1;97mCancel\033[0m")
                        
                        menu = "\n".join(menu_lines)
                        display_paged_menu(menu)
                        
                        del_sel = input("\nWhich instance do you want to delete? (Enter number or name): ").strip()
                        if del_sel.lower() == 'cancel' or del_sel == str(len(existing_instances)+1):
                            del_target = None
                            break
                        try:
                            del_idx = int(del_sel) - 1
                            if 0 <= del_idx < len(existing_instances):
                                del_target = existing_instances[del_idx]
                                break
                        except ValueError:
                            if del_sel in existing_instances:
                                del_target = del_sel
                                break
                        print(f"\n[ ❌ ] \033[1;91mInvalid selection.\033[0m")
                else:
                    if del_selected.get('type') == 'instance':
                        del_target = del_selected['id']
                        
                if not del_target:
                    print(f"\n[ ℹ️ ] Deletion aborted.\n")
                    time.sleep(1)
                    continue
                    
                print(f"\n[ ⚠️ ] \033[1;93mWARNING: You are about to PERMANENTLY DELETE the instance '{del_target}' and all its contents (saves, mods, etc).\033[0m")
                confirm = input(f"       Type the exact name of the instance ('{del_target}') to confirm deletion: ").strip()
                if confirm == del_target:
                    inst_path = os.path.join(MC_DIR, "instances", del_target)
                    try:
                        shutil.rmtree(inst_path)
                        print(f"[ ✅ ] \033[1;92mInstance '{del_target}' has been completely deleted.\033[0m")
                        time.sleep(1)
                    except Exception as e:
                        print(f"[ ❌ ] \033[1;91mError deleting instance:\033[0m {e}")
                        sys.exit(1)
                    continue
                else:
                    print(f"\n[ ℹ️ ] Deletion aborted.\n")
                    time.sleep(1)
                    continue
            
            # If not deleting, break to proceed with the selected instance/creation
            break
        
        if selected_instance['type'] == 'instance':
            # Existing instance selected
            INSTANCE_NAME = selected_instance['id']
            VERSION = get_instance_version(INSTANCE_NAME)
            if not VERSION:
                print(f"\n[ ❌ ] \033[1;91mInstance '{INSTANCE_NAME}' has no version installed.\033[0m")
                sys.exit(1)
    
    creating_loader = None
    if not VERSION:
        if selected_instance and selected_instance['type'].startswith('create_'):
            creating_loader = selected_instance['type'].split('_')[1]
            logger.info(f"\n[ 🔧 ] \033[1;95m{creating_loader.capitalize()} Installation Flow Initiated.\033[0m")
        try:
            # SUG-004: honour the manifest cache TTL (MANIFEST_TTL_S) unless --refresh
            _cache_stale = (not os.path.exists(manifest_cache)) or (time.time() - os.path.getmtime(manifest_cache) > MANIFEST_TTL_S)
            if args.refresh or _cache_stale:
                manifest_json_remote_source1 = b64d('aHR0cHM6Ly9sYXVuY2hlcm1ldGEubW9qYW5nLmNvbS9tYy9nYW1lL3ZlcnNpb25fbWFuaWZlc3QuanNvbg==')
                manifest_json_remote_source2 = b64d('aHR0cHM6Ly9waXN0b24tbWV0YS5tb2phbmcuY29tL21jL2dhbWUvdmVyc2lvbl9tYW5pZmVzdC5qc29u')
                try:
                    r = net_client.get(manifest_json_remote_source1, timeout=15)
                    r.raise_for_status()
                except Exception as err1:
                    logger.warning(f"[ ⚠️ ] Primary version manifest server unreachable: {err1}. Trying fallback server...")
                    try:
                        r = net_client.get(manifest_json_remote_source2, timeout=15)
                        r.raise_for_status()
                    except Exception as err2:
                        logger.error(f"[ ❌ ] Fallback server also failed: {err2}")
                        raise err2
                
                manifest = r.json()
                with open(manifest_cache, 'w', encoding='utf-8') as f: json.dump(manifest, f)
            else:
                try:
                    with open(manifest_cache, 'r', encoding='utf-8') as f: manifest = json.load(f)
                    if not isinstance(manifest.get('versions'), list): raise ValueError("missing 'versions' list")
                except Exception as cache_err:
                    print(f"[ ❌ ] Cached version manifest is corrupt: {cache_err}")
                    sys.exit(1)
        except Exception as main_e:
            if os.path.exists(manifest_cache):
                try:
                    logger.warning(f"[ ⚠️ ] Network error occurred ({main_e}). Using cached version manifest.")
                    with open(manifest_cache, 'r', encoding='utf-8') as f: manifest = json.load(f)
                    if not isinstance(manifest.get('versions'), list): raise ValueError("missing 'versions' list")
                except Exception as cache_err:
                    print(f"[ ❌ ] Failed to fetch version manifest and cached manifest is corrupt: {cache_err}")
                    sys.exit(1)
            else:
                print(f"[ ❌ ] Failed to fetch version manifest and no cache available: {main_e}")
                sys.exit(1)
    
        v_pool = [v for v in manifest['versions'] if v['type'] in (['snapshot'] if args.snapshots else (['old_beta', 'old_alpha'] if args.beta else ['release']))]
        last_saved = ""
        last_ver_file = os.path.join(MC_DIR, "cache/last_version.txt")
        try:
            if os.path.exists(last_ver_file):
                with open(last_ver_file, 'r', encoding='utf-8') as f: last_saved = f.read().strip()
        except Exception: pass
    
        warning_msg = ""
        if last_saved and not any(v['id'] == last_saved for v in v_pool):
            warning_msg = f"[ ⚠️ ] \033[1;93mWARNING: Last played version '{last_saved}' is hidden by current filters.\033[0m"
    
        # Version menu
        # Interactive Menu setup
        def interactive_select(options, last_saved="", warning_msg=""):
            if not options:
                logger.error("[ ❌ ] No versions found. Check your filters.")
                sys.exit(1)

            if sys.stdout.isatty() and options:
                total = len(options)
                curr = 0
        
                if last_saved:
                    for i, v in enumerate(options):
                        if v['id'] == last_saved:
                            curr = i
                            break
            
                while True:
                    try:
                        term_height = os.get_terminal_size().lines
                        window_size = max(5, term_height - 8)
                    except Exception:
                        window_size = 15
        
                    if ansi_clear:
                        buf = ["\033[H\033[J\n\033[1;96m------ Choose Game version ------\033[0m\n"]
                    else:
                        os.system('cls' if is_windows else 'clear')
                        buf = ["\n\033[1;96m------ Choose Game version ------\033[0m\n"]
                    
                    if warning_msg: buf.append(f"  {warning_msg}\n")
                    less_hint = 'Use less / ' if shutil.which("less") else ''
                    buf.append(f"\033[1;97mNavigate: \033[1;96mArrows\033[1;97m ( \033[1;96m↑\033[1;97m and \033[1;96m↓\033[1;97m ) | \033[1;97mSelect: \033[1;96mEnter\033[1;97m | \033[1;97mBack: \033[1;96mEsc\033[1;97m | \033[1;97m{less_hint}Print Mode: \033[1;96mQ\033[1;97m\n\n")
        
                    start = max(0, min(curr - window_size // 2, total - window_size))
                    end = min(start + window_size, total)
        
                    for i in range(start, end):
                        v = options[i]
                        is_selected = (i == curr)
                        is_last = (v['id'] == last_saved)
        
                        sel_marker = "  [ \033[1;96mX\033[0m ]\033[1;96m " if is_selected else "  [   ]\033[1;97m "
        
                        line = f"{sel_marker}{v['id']}\033[0m (\033[1;93m{v['type']}\033[0m)\033[0m"
                        if is_last:
                            line += "  \033[1;91m<-- (Last Selected)\033[0m"
        
                        buf.append(line + "\n")
        
                    buf.append(f"\n  [ \033[1;94m{curr + 1}\033[0m / \033[1;94m{total}\033[0m ] | Page: \033[1;94m{start+1}\033[0m-\033[1;94m{end}\033[0m\n")
                    
                    sys.stdout.write("".join(buf))
                    sys.stdout.flush()
        
                    if is_windows:
                        key = msvcrt.getch()
                        if key in (b'\xe0', b'\x00'):
                            key = msvcrt.getch()
                            if key == b'H': curr = max(0, curr - 1)
                            elif key == b'P': curr = min(total - 1, curr + 1)
                        elif key in (b'\r', b'\n'):
                            return options[curr]
                        elif key in (b'\x1b', b'\x03'):
                            return "__back__"
                        elif key.lower() == b'q':
                            os.system('cls')
                            return None
                    else:
                        key = _read_posix_key()
                        if key == '\x1b[A': curr = max(0, curr -1)
                        elif key == '\x1b[B': curr = min(total - 1, curr + 1)
                        elif key in ('\r', '\n'): return options[curr]
                        elif key in ('\x1b', '\x03'):
                            return "__back__"
                        elif key.lower() == 'q':
                            print("\033[H\033[J", end="")
                            return None
            return None

        # VERSION SELECTION LOOP (with Esc-to-go-back to Instance page)
        while True:
            if HEADLESS_CREATE is not None:
                # Headless creation: skip version selection entirely (hoisted ABOVE the
                # TUI call so --create-instance never renders a menu or blocks on keys,
                # and an empty v_pool cannot sys.exit(1) before the break fires).
                VERSION = HEADLESS_CREATE['version']
                INSTANCE_NAME = HEADLESS_CREATE['name']
                print(f"[ ✅ ] Headless create: '\033[1;92m{INSTANCE_NAME}\033[0m' ({HEADLESS_CREATE['loader']} {VERSION})")
                break
            
            selected_obj = interactive_select(v_pool, last_saved, warning_msg)
            
            
            if selected_obj == "__back__":
                # Go back to instance selection
                existing_instances = list_instances()
                selected_instance = interactive_instance_select(existing_instances, last_saved_instance)
                if selected_instance is None:
                    # Q-key fallback (parity with the initial instance selection): numbered print mode
                    while True:
                        _bl = ["\n\033[1;96m  ---- INSTANCE LIST ----\033[0m"]
                        for _bi, _binst in enumerate(existing_instances):
                            _bl.append(f"    \033[1;96m{_bi+1}\033[0m. \033[1;97m{_binst}\033[0m ({get_instance_version(_binst) or 'empty'})")
                        _bmenu = "\n".join(_bl)
                        display_paged_menu(_bmenu)
                        try:
                            _bsel = input("\n\033[1;97mSelect Instance\033[0m (blank = exit): ").strip()
                        except (EOFError, KeyboardInterrupt):
                            print("\n[ 💀 ] Shutdown requested by user. BYE...\n")
                            sys.exit(0)
                        if not _bsel:
                            print("\n[ 💀 ] Shutdown requested by user. BYE...\n")
                            sys.exit(0)
                        try:
                            _bidx = int(_bsel) - 1
                            if 0 <= _bidx < len(existing_instances):
                                selected_instance = {'id': existing_instances[_bidx], 'type': 'instance'}
                                break
                        except ValueError:
                            if _bsel in existing_instances:
                                selected_instance = {'id': _bsel, 'type': 'instance'}
                                break
                        print("[ ❌ ] \033[1;91mInvalid selection.\033[0m")
                if selected_instance['type'] == 'instance':
                    INSTANCE_NAME = selected_instance['id']
                    VERSION = get_instance_version(INSTANCE_NAME)
                    if not VERSION:
                        print(f"\n[ ❌ ] \033[1;91mInstance '{INSTANCE_NAME}' has no version installed.\033[0m")
                        sys.exit(1)
                    break
                # Update loader intent if the user picked a different create option
                if selected_instance['type'].startswith('create_'):
                    creating_loader = selected_instance['type'].split('_')[1]
                continue
            elif selected_obj is None:
                # Fallback to print mode (Q pressed)
                while True:
                    if warning_msg: print(f"\n  {warning_msg}")
                    print("\n\033[1;96m  ---- Game VERSION LIST ----\033[0m")
                    menu = "\n".join([f"    \033[1;96m{i+1}\033[0m. \033[1;97m{v['id']}\033[0m (\033[1;93m{v['type']}\033[0m)" for i, v in enumerate(v_pool)])
                    display_paged_menu(menu)
                    
                    default_sel = v_pool[0]['id'] if v_pool else ""
                    sel = input(f"\n    \033[1;97mSelect Version\033[0m{f'\033[0m [ Default: \033[1;94m{default_sel}\033[0m ]' if default_sel else ''}:\033[0m ").strip()
                    if not sel and default_sel:
                        VERSION = default_sel
                        V_URL = next(v['url'] for v in v_pool if v['id'] == VERSION)
                        break
                    try:
                        idx = int(sel) - 1
                        if 0 <= idx < len(v_pool):
                            VERSION, V_URL = v_pool[idx]['id'], v_pool[idx]['url']
                            break
                    except Exception:
                        pass
                break
            else:
                VERSION, V_URL = selected_obj['id'], selected_obj['url']
                break
        
        # INSTANCE NAMING (for new instances via Questionary)
        if not INSTANCE_NAME:
            if ansi_clear: sys.stdout.write("\033[H\033[J")
            else: os.system('cls')
            
            if creating_loader:
                suffix = "NeoForge" if creating_loader == "neoforge" else creating_loader.capitalize()
                base_default = f"{VERSION}-{suffix}"
            else:
                base_default = VERSION
                
            # Find a free default name
            default_name = base_default
            counter = 1
            while os.path.exists(os.path.join(MC_DIR, "instances", default_name)):
                default_name = f"{base_default}-{counter}"
                counter += 1
                
            while True:
                try:
                    if 'questionary' in sys.modules:
                        inst_name_raw = questionary.text(
                            "Name your instance:",
                            default=default_name,
                        ).ask()
                        if inst_name_raw is None:
                            print("\n[ 💀 ] Shutdown requested by user. BYE...")
                            sys.exit(0)
                    else:
                        print(f"       \033[1;90m(Leave blank to use default: {default_name})\033[0m")
                        inst_name_raw = input("? Name your instance: ")
                except KeyboardInterrupt:
                    print("\n[ 💀 ] Shutdown requested by user. BYE...")
                    sys.exit(0)
                except Exception:
                    inst_name_raw = default_name
                
                if not inst_name_raw: inst_name_raw = default_name
                
                if not is_valid_instance_name(inst_name_raw):
                    print(f"\n[ ❌ ] \033[1;91mInvalid instance name: '{inst_name_raw}'.\033[0m")
                    print("       \033[1;93mNames cannot contain \\ / : * ? \" < > | or be reserved OS names (like CON, PRN).\033[0m\n")
                    continue
                    
                INSTANCE_NAME = inst_name_raw.strip().rstrip(' .')
                
                if os.path.exists(os.path.join(MC_DIR, "instances", INSTANCE_NAME)):
                    print(f"\n[ ❌ ] \033[1;91mInstance '{INSTANCE_NAME}' already exists. Please choose a different name.\033[0m\n")
                    # Update default_name to what they just typed + counter logic so they can see an alternative next time
                    base_typed = INSTANCE_NAME
                    c = 1
                    while os.path.exists(os.path.join(MC_DIR, "instances", f"{base_typed}-{c}")):
                        c += 1
                    default_name = f"{base_typed}-{c}"
                    continue
                else:
                    try:
                        create_instance_dirs(INSTANCE_NAME)
                        break
                    except OSError as e:
                        print(f"\n[ ❌ ] \033[1;91mOS Error: Cannot create instance '{INSTANCE_NAME}'.\033[0m")
                        print(f"       \033[1;93mReason: {e}\033[0m")
                        print("[ ℹ️ ] \033[1;93mPlease choose a different, valid name.\033[0m\n")
                        continue
            
            if creating_loader in ('fabric', 'quilt'):
                is_quilt = (creating_loader == 'quilt')
                base_url = "https://meta.quiltmc.org/v3" if is_quilt else "https://meta.fabricmc.net/v2"
                name_cap = "Quilt" if is_quilt else "Fabric"
                try:
                    logger.info(f"\n[ 🌐 ] \033[1;95mFetching {name_cap} Loaders for {VERSION}...\033[0m")
                    r = net_client.get(f"{base_url}/versions/loader/{VERSION}")
                    loaders = [L['loader']['version'] for L in r.json()]
                    if is_quilt:
                        loaders.sort(key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)], reverse=True)
                    if not loaders:
                        logger.error(f"[ ❌ ] \033[1;91mNo {name_cap} loaders found for {VERSION}.\033[0m")
                        sys.exit(1)
                    
                    loader_options = [{"id": l, "display": f"{name_cap} Loader \033[1;95m{l}\033[0m", "type": creating_loader} for l in loaders]
                    print(f"\n\033[1;96m------ Select {name_cap} Loader ------\033[0m")
                    sel_loader_obj = interactive_select(loader_options) if sys.stdout.isatty() else {"id": loaders[0]}
                    if not sel_loader_obj or sel_loader_obj == "__back__":
                        logger.error(f"\n[ 💀 ] \033[1;91m{name_cap} installation cancelled.\033[0m")
                        sys.exit(1)
                    
                    loader_ver = sel_loader_obj['id']
                    loader_id = f"{creating_loader}-loader-{loader_ver}-{VERSION}"
                    
                    logger.info(f"[ 📦 ] \033[1;94mGenerating {name_cap} Profile ({loader_id})...\033[0m")
                    prof_r = net_client.get(f"{base_url}/versions/loader/{VERSION}/{loader_ver}/profile/json")
                    prof_json = prof_r.json()
                    
                    v_dir = os.path.join(MC_DIR, "instances", INSTANCE_NAME, "versions", loader_id)
                    os.makedirs(v_dir, exist_ok=True)
                    with open(os.path.join(v_dir, f"{loader_id}.json"), 'w', encoding='utf-8') as f:
                        json.dump(prof_json, f, indent=4)
                    
                    VERSION = loader_id
                except Exception as e:
                    logger.error(f"\n[ ❌ ] \033[1;91m{name_cap} Meta API failed: {e}\033[0m")
                    if is_quilt:
                        logger.error("Quilt headless fallback not implemented.")
                        sys.exit(1)
                    logger.warning("[ ⚠️ ] Attempting Fabric Fallback Headless Installation...")
                    try:
                        try:
                            meta_r = net_client.get("https://meta.fabricmc.net/v2/versions/installer")
                            installer_url = meta_r.json()[0]['url']
                            with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'w') as f: f.write(installer_url)
                        except Exception:
                            try:
                                import xml.etree.ElementTree as ET
                                xml_r = net_client.get("https://maven.fabricmc.net/net/fabricmc/fabric-installer/maven-metadata.xml")
                                root = ET.fromstring(xml_r.text)
                                latest_ver = root.find('.//latest').text
                                installer_url = f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/{latest_ver}/fabric-installer-{latest_ver}.jar"
                                with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'w') as f: f.write(installer_url)
                            except Exception:
                                try:
                                    with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'r') as f: installer_url = f.read().strip()
                                except Exception:
                                    raise Exception("Could not resolve Fabric installer URL.")
                        installer_path = os.path.join(MC_DIR, "cache", "fabric-installer.jar")
                        get(installer_url, installer_path)
                        java_bin = get_installer_java(17)
                        try:
                            subprocess.run([java_bin, "-jar", installer_path, "client", "-dir", os.path.join(MC_DIR, "instances", INSTANCE_NAME), "-mcversion", VERSION, "-noprofile"], check=True, capture_output=True, text=True)
                        except subprocess.CalledProcessError as e:
                            logger.error(f"[ ❌ ] \033[1;91mInstaller failed with exit code {e.returncode}:\033[0m\n{e.stderr or e.stdout}")
                            sys.exit(1)
                        logger.info(f"\n[ ✅ ] \033[1;92mFallback installation complete. Update .primary_version manually.\033[0m")
                        sys.exit(0)
                    except Exception as fallback_e:
                        logger.error(f"[ ❌ ] \033[1;91mFallback failed: {fallback_e}\033[0m")
                        logger.warning("[ 🗑️ ] Cleaning up incomplete instance...")
                        shutil.rmtree(os.path.join(MC_DIR, "instances", INSTANCE_NAME), ignore_errors=True)
                        sys.exit(1)
            elif creating_loader in ('forge', 'neoforge'):
                is_neo = (creating_loader == 'neoforge')
                name_cap = "NeoForge" if is_neo else "Forge"
                try:
                    logger.info(f"\n[ 🌐 ] \033[1;95mFetching {name_cap} Versions for {VERSION}...\033[0m")
                    if is_neo:
                        r = net_client.get("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
                        all_vers = r.json()['versions']
                        # NeoForge versioning: MC 1.20.4 -> 20.4.x
                        prefix = VERSION[2:] + '.'
                        valid_vers = [v for v in all_vers if v.startswith(prefix)]
                        if not valid_vers:
                            logger.error(f"[ ❌ ] \033[1;91mNo {name_cap} versions found for {VERSION}.\033[0m")
                            sys.exit(1)
                        # Pick latest by sorting
                        valid_vers.sort(key=lambda x: [int(p) if p.isdigit() else p for p in x.split('.')])
                        loader_ver = valid_vers[-1]
                        installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_ver}/neoforge-{loader_ver}-installer.jar"
                        loader_id = f"neoforge-{loader_ver}"
                    else:
                        _forge_promos_url = b64d('aHR0cHM6Ly9maWxlcy5taW5lY3JhZnRmb3JnZS5uZXQvbmV0L21pbmVjcmFmdGZvcmdlL2ZvcmdlL3Byb21vdGlvbnNfc2xpbS5qc29u')
                        _forge_maven_base = b64d('aHR0cHM6Ly9tYXZlbi5taW5lY3JhZnRmb3JnZS5uZXQvbmV0L21pbmVjcmFmdGZvcmdlL2ZvcmdlLw==')
                        r = net_client.get(_forge_promos_url)
                        promos = r.json()['promos']
                        target_key = f"{VERSION}-recommended"
                        if target_key not in promos: target_key = f"{VERSION}-latest"
                        if target_key not in promos:
                            logger.error(f"[ ❌ ] \033[1;91mNo {name_cap} versions found for {VERSION}.\033[0m")
                            sys.exit(1)
                        loader_ver = promos[target_key]
                        installer_url = f"{_forge_maven_base}{VERSION}-{loader_ver}/forge-{VERSION}-{loader_ver}-installer.jar"
                        loader_id = f"forge-{VERSION}-{loader_ver}"
                    
                    logger.info(f"[ 📦 ] \033[1;94mDownloading {name_cap} Installer ({loader_id})...\033[0m")
                    installer_path = os.path.join(MC_DIR, "cache", f"{loader_id}-installer.jar")
                    get(installer_url, installer_path)
                    
                    logger.info(f"[ ☕ ] \033[1;93mRunning {name_cap} Installer Headlessly...\033[0m")
                    
                    # Spoof vanilla directory structure inside the instance folder to trick the installer
                    inst_root = os.path.join(MC_DIR, "instances", INSTANCE_NAME)
                    os.makedirs(os.path.join(inst_root, "versions"), exist_ok=True)
                    os.makedirs(os.path.join(inst_root, "libraries"), exist_ok=True)
                    with open(os.path.join(inst_root, "launcher_profiles.json"), 'w') as f: f.write("{}")
                    
                    java_bin = get_installer_java(17)
                    try:
                        subprocess.run([java_bin, "-jar", installer_path, "--installClient", inst_root], check=True, capture_output=True, text=True)
                    except subprocess.CalledProcessError as e:
                        logger.error(f"[ ❌ ] \033[1;91mInstaller failed with exit code {e.returncode}:\033[0m\n{e.stderr or e.stdout}")
                        sys.exit(1)
                    
                    # Forge installer puts the version JSON in instances/<name>/versions/<loader_id>/<loader_id>.json
                    # But it might be named slightly differently (e.g. forge-1.20.1). Let's scan for it.
                    v_dirs = [d for d in os.listdir(os.path.join(inst_root, "versions")) if os.path.isdir(os.path.join(inst_root, "versions", d))]
                    if loader_id in v_dirs:
                        VERSION = loader_id
                    elif v_dirs:
                        # Sort by modification time and pick the newest
                        v_dirs.sort(key=lambda d: os.path.getmtime(os.path.join(inst_root, "versions", d)))
                        VERSION = v_dirs[-1]
                    else:
                        VERSION = loader_id
                    
                except Exception as e:
                    logger.error(f"\n[ ❌ ] \033[1;91m{name_cap} Installation failed: {e}\033[0m")
                    logger.warning("[ 🗑️ ] Cleaning up incomplete instance...")
                    shutil.rmtree(os.path.join(MC_DIR, "instances", INSTANCE_NAME), ignore_errors=True)
                    sys.exit(1)
            
            with open(os.path.join(MC_DIR, "instances", INSTANCE_NAME, ".primary_version"), 'w', encoding='utf-8') as f:
                f.write(VERSION)
            print(f"\n[ ✅ ] \033[1;92mCreated instance:\033[0m \033[1;97m{INSTANCE_NAME}\033[0m")

    
    # Save last played instance
    if not args.check_java and not args.dry_run:
        with open(last_inst_file, 'w', encoding='utf-8') as f: f.write(INSTANCE_NAME)
    
    # CHECK RUNTIME ASSETS & NATIVES
    INST_DIR = os.path.join(MC_DIR, "instances", INSTANCE_NAME)
    add_instance_log(INSTANCE_NAME)
    
    # Load Instance Profile (CLI flags take precedence over profile defaults)
    prof_dict, _ = load_instance_profile(INST_DIR, args.profile)
    if prof_dict:
        if "memory" in prof_dict and args.memory == '2G': MEMORY = prof_dict["memory"]
        if "jvm_flags" in prof_dict and not args.jvm_flags.strip(): JVM_ARGS = prof_dict["jvm_flags"]
        if "game_flags" in prof_dict and not args.game_flags.strip(): GAME_ARGS = prof_dict["game_flags"]
        if "java" in prof_dict and prof_dict["java"]: INSTANCE_JAVA = prof_dict["java"]   # <-- Phase 2
    
    # Run Crash Log Analyzer
    analyze_crash_logs(INST_DIR)
    # ---- PARITY-001: SKIN & CAPE INJECTION (CustomSkinLoader) ----
    if getattr(args, 'set_skin', None) or getattr(args, 'set_cape', None):
        handle_skin_cape_flags()
        # BUG-125: a reborn rewrites .primary_version in place; refresh VERSION so this
        # same launch resolves the new modded JSON (Report 18 Block 3 design intent).
        VERSION = get_instance_version(INSTANCE_NAME) or VERSION
    # LOCAL JSON RESOLUTION & INHERITANCE ENGINE
    manifest_cache_data = None

    def resolve_version_json(version_id, v_url=None, depth=0):
        global manifest_cache_data
        if depth > 10:
            raise RuntimeError(f"Circular JSON inheritance detected for version '{version_id}'")
            
        v_dir = os.path.join(INST_DIR, f"versions/{version_id}")
        if not STERILE and not args.dry_run: safe_makedirs(v_dir, is_file=False)
        v_path = os.path.join(v_dir, f"{version_id}.json")
        
        # Download if missing (never overwrites instance JSONs, respecting Phase 2 rules)
        if not args.dry_run and not os.path.exists(v_path):
            if v_url:
                try:
                    get(v_url, v_path, silent=True)
                except RuntimeError:
                    # get() raises under offline mode when the file is missing -> fall through to graceful EXIT_FILES
                    pass
            else:
                if not manifest_cache_data and os.path.exists(manifest_cache):
                    try:
                        with open(manifest_cache, 'r', encoding='utf-8') as f:
                            manifest_cache_data = json.load(f)
                    except Exception:
                        manifest_cache_data = None
                        
                _v_url = next((v['url'] for v in manifest_cache_data.get('versions', []) if v['id'] == version_id), None) if manifest_cache_data else None
                if _v_url:
                    try:
                        get(_v_url, v_path, silent=True)
                    except RuntimeError:
                        # get() raises under offline mode when the file is missing -> fall through to graceful EXIT_FILES
                        pass
                else:
                    logger.error(f"[ ❌ ] Failed to locate parent version '{version_id}' in the version manifest.")
                    sys.exit(1)
        if not os.path.exists(v_path):
            if args.offline:
                print(f"[ ❌ ] \033[1;91mOffline mode: version metadata for '{version_id}' is not cached in this instance. Launch this instance once online to fetch it.\033[0m")
            else:
                print(f"[ ❌ ] \033[1;91mVersion metadata for '{version_id}' is missing and could not be downloaded.\033[0m")
            sys.exit(EXIT_FILES)
        with open(v_path, 'r', encoding='utf-8') as f:
            v_data = json.load(f)
            
        if 'inheritsFrom' in v_data:
            parent_id = v_data['inheritsFrom']
            parent_data = resolve_version_json(parent_id, depth=depth+1)
            
            # 1. Merge libraries
            if 'libraries' in v_data:
                if 'libraries' not in parent_data:
                    parent_data['libraries'] = []
                parent_data['libraries'].extend(v_data['libraries'])
            
            # 2. Merge arguments (append child to parent)
            if 'arguments' in parent_data and 'arguments' in v_data:
                for arg_type in ['game', 'jvm']:
                    if arg_type in v_data['arguments']:
                        if arg_type not in parent_data['arguments']:
                            parent_data['arguments'][arg_type] = []
                        parent_data['arguments'][arg_type].extend(v_data['arguments'][arg_type])
            elif 'arguments' in v_data:
                parent_data['arguments'] = v_data['arguments']
            
            # 3. Merge legacy game arguments (obfuscated key)
            _mc_args_key = _GAME_ARGS_KEY
            if _mc_args_key in v_data:
                if _mc_args_key in parent_data:
                    parent_data[_mc_args_key] += " " + v_data[_mc_args_key]
                else:
                    parent_data[_mc_args_key] = v_data[_mc_args_key]
            
            # 4. Preserve parent JAR name before overwriting simple keys
            parent_data['jar'] = parent_data.get('jar', parent_data['id'])
            
            for key in ['mainClass', 'assetIndex', 'downloads', 'javaVersion', 'id', 'jar']:
                if key in v_data:
                    parent_data[key] = v_data[key]
                    
            return parent_data
        return v_data

    v_json = resolve_version_json(VERSION, V_URL)
    v_mjvn = max(8, v_json.get('javaVersion', {}).get('majorVersion', 8))
    
    if args.check_java:
        print(f"\n  [ ☕ ] \033[1;97mRequired major Java Version for {VERSION}:\033[0m \033[1;92mJava {v_mjvn}\033[0m")
        if is_windows:
            print(f"  [ 🔧 ] \033[1;97mInstallation Path (.exe):\033[0m \033[1;96m{INST_DIR}\033[0m\n")
        else:
            print(f"  [ 🔧 ] \033[1;97mInstallation Path (.jar):\033[0m \033[1;96m{INST_DIR}\033[0m\n")
        sys.exit(0)
    
    # Phase 4 Rolling Backup Execution
    target_json = os.path.join(INST_DIR, f"versions/{VERSION}/{VERSION}.json")
    if os.path.exists(target_json) and not args.dry_run:
        with open(target_json, "rb") as f:
            current_hash = hashlib.sha256(f.read()).hexdigest()
        hash_file = os.path.join(INST_DIR, ".last_hash")
        old_hash = ""
        if os.path.exists(hash_file):
            with open(hash_file, "r") as f: old_hash = f.read().strip()
            
        if current_hash != old_hash:
            with open(hash_file, "w") as f: f.write(current_hash)
            
            # Only backup if it's an actual modification (old_hash exists)
            if old_hash and max_backups > 0:
                backup_dir = os.path.join(MC_DIR, "backups", INSTANCE_NAME)
                os.makedirs(backup_dir, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_file = os.path.join(backup_dir, f"{VERSION}_{ts}.json")
                shutil.copy2(target_json, backup_file)
                
                backups = sorted([os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.json')], key=os.path.getmtime)
                while len(backups) > max_backups:
                    os.remove(backups.pop(0))
    
    v_root = os.path.join(INST_DIR, f"versions/{VERSION}")
    integrity_marker = os.path.join(v_root, ".integrity_passed")
    

    # Fabric JSONs often use the 'jar' property to point to the parent vanilla JAR
    jar_version = v_json.get('jar', VERSION)
    jar_dir = os.path.join(INST_DIR, f"versions/{jar_version}")
    if not args.dry_run: os.makedirs(jar_dir, exist_ok=True)
    jar_path = os.path.join(jar_dir, f"{jar_version}.jar")
    
    # Only download jar if marker is missing or recheck is enforced
    if (not os.path.exists(integrity_marker) or args.recheck) and not args.offline and not args.dry_run:
        dl_client = v_json.get('downloads', {}).get('client')
        if dl_client and 'url' in dl_client:
            get(dl_client['url'], jar_path, dl_client.get('sha1'))
    
    cp_paths, lib_queue, natives_queue = [jar_path], [], []
    
    # Mapping variables
    natives_dir = os.path.join(v_root, 'natives')
    if not args.dry_run: os.makedirs(natives_dir, exist_ok=True)
    
    # Parse Libraries
    for lib in v_json.get('libraries', []):
        if not is_allowed(lib.get('rules')): continue
        dl = lib.get('downloads', {})
        if 'artifact' in dl:
            lp = os.path.join(MC_DIR, "libraries", dl['artifact']['path'])
            lib_queue.append((dl['artifact']['url'], lp, dl['artifact'].get('sha1')))
            cp_paths.append(lp)
        elif 'name' in lib:
            parts = lib['name'].split(':')
            if len(parts) >= 3:
                g, a, v = parts[0], parts[1], parts[2]
                path = f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}.jar"
                local_lp = os.path.join(INST_DIR, "libraries", path.replace('/', os.sep))
                global_lp = os.path.join(MC_DIR, "libraries", path.replace('/', os.sep))
                
                if os.path.exists(local_lp):
                    lp = local_lp
                else:
                    lp = global_lp
                
                url = lib.get('url')
                if url:
                    if not url.endswith('/'): url += '/'
                    url += path
                    lib_queue.append((url, lp, None))
                cp_paths.append(lp)
                
        # Explicitly look for OS natives
        if f"natives-{platform_os}" in dl.get('classifiers', {}):
            n_data = dl['classifiers'][f"natives-{platform_os}"]
            np = os.path.join(MC_DIR, "libraries", n_data['path'])
            lib_queue.append((n_data['url'], np, n_data.get('sha1')))
            natives_queue.append(np)
    
    asset_index = v_json.get('assetIndex', {})
    a_id = asset_index.get('id')
    asset_q = []
    
    # Prepare asset queue
    if a_id:
        a_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
        if not args.offline and not args.dry_run:
            if (not os.path.exists(integrity_marker) or args.recheck) and 'url' in asset_index:
                get(asset_index['url'], a_path, asset_index.get('sha1'), silent=True)
            if os.path.exists(a_path):
                with open(a_path, 'r', encoding='utf-8') as f:
                    objs = json.load(f).get('objects', {})
                    res_link = b64d("aHR0cHM6Ly9yZXNvdXJjZXMuZG93bmxvYWQubWluZWNyYWZ0Lm5ldA==")
                    asset_q = [(f"{res_link}/{h[:2]}/{h}", os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}"), h) for h in [d['hash'] for d in objs.values()]]
    
    # INTEGRITY CHECK, RETRY & SUCCESS MARKER
    if args.dry_run:
        print(f"[ 🧪 ] \033[1;96mDry-run mode:\033[0m \033[1;97mSkipping downloads for VERSION:\033[0m \033[1;92m{VERSION}\033[0m")
    elif args.offline or (os.path.exists(integrity_marker) and not args.recheck):
        print(f"[ ✅ ] \033[1;92mIntegrity marker found.\033[0m \033[1;97mSkipping verification for VERSION:\033[0m \033[1;92m{VERSION}\033[0m")
    else:
        max_retries = 7
        success = False
    
        for attempt in range(max_retries):
            print(f"\n[ \033[1;95m{attempt+1}\033[0m 🎯 ] \033[1;97mDownload/Verification Attempt:\033[0m ( \033[1;95m{attempt+1}\033[0m / \033[1;95m{max_retries}\033[0m )")
    
            def safe_batch_get(args_tuple):
                try:
                    get(args_tuple[0], args_tuple[1], args_tuple[2], silent=True)
                except Exception as e:
                    logger.debug(f"Download failed for {os.path.basename(args_tuple[1])}: {e}")

            # Run Downloads
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                if lib_queue:
                    list(tqdm(ex.map(safe_batch_get, lib_queue), total=len(lib_queue), desc="  [ 🔍 ] \033[1;94mDownloading & Verifying Libs\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} \033[0m files  "))
                if asset_q:
                    list(tqdm(ex.map(safe_batch_get, asset_q), total=len(asset_q), desc="  [ 🔍 ] \033[1;94mDownloading & Verifying Assets\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} \033[0m items  "))
    
            # Final Integrity Check
            missing = []
            for _, path, _ in lib_queue:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
            for _, path, _ in asset_q:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
    
            if not missing:
                print("[ ✅ ] \033[1;92mAll files verified successfully.\033[0m")
                success = True
                break
            else:
                print(f"[ ⚠️ ] \033[1;93mWarning:\033[0m {len(missing)} file/s failed to download or are corrupt:")
                for m in missing[:15]: # Log first 15 missing files to stdout
                    print(f" - {os.path.basename(m)}")
                if len(missing) > 15: print(f" ... and {len(missing)-15} more.")
    
                if attempt < max_retries - 1:
                    print("[ ⚠️ ] \033[1;93mRetrying missing files in 5 seconds...\033[0m")
                    time.sleep(5)
        
        if not success:
            print("\n[ ❌ ] \033[1;91mCritical Error:\033[0m Failed to download required files after multiple attempts.")
            print(f"[ ❌ ] {len(missing)} files are still missing. \033[1;91mAborting launch.\033[0m")
            sys.exit(1)
    
    if args.old_compatibility and not args.dry_run:
        # Sound compatibility fix for old versions
        asset_index_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
        if os.path.exists(asset_index_path):
            with open(asset_index_path, 'r', encoding='utf-8') as f:
                index_data = json.load(f)
                objects = index_data.get('objects', {})
                
                # tqdm for visual feedback on sound mapping
                _resources_root = os.path.abspath(os.path.join(MC_DIR, "resources"))
                for name, info in tqdm(objects.items(), desc="[ 🔊 ] \033[1;94mReconstructing Legacy Sounds\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}\033[0m items  "):
                    h = info['hash']
                    src_file = os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}")
                    dst_file = os.path.join(MC_DIR, "resources", name)
                    
                    if not os.path.abspath(dst_file).startswith(_resources_root + os.sep):
                        logger.warning(f"[ ⚠️ ] Skipping suspicious legacy asset path: {name}")
                        continue
                    
                    if os.path.exists(src_file):
                        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                        if not os.path.exists(dst_file):
                            shutil.copy2(src_file, dst_file)
    
    # Extract natives
    if args.recheck and not args.dry_run and os.listdir(natives_dir):
        for f_n in os.listdir(natives_dir): os.remove(os.path.join(natives_dir, f_n))
    if not args.dry_run and not os.listdir(natives_dir):
        print(f"[ 📂 ] \033[1;97mExtracting Natives...\033[0m ({platform_os})")
        for np in natives_queue:
            if os.path.exists(np):
                try:
                    with zipfile.ZipFile(np, 'r') as z:
                        ext = '.dll' if is_windows else ('.dylib' if is_mac else '.so')
                        for n in [f for f in z.namelist() if f.endswith(ext)]:
                            with z.open(n) as s, open(os.path.join(natives_dir, os.path.basename(n)), "wb") as d: d.write(s.read())
                except Exception: pass
    
    # ATTENTION NEEDED!!! (For linux only) Specifically extract libflite.so from the text2speech library if found
    if not args.dry_run and platform_os == "linux":
        for lp in cp_paths:
            if "text2speech" in lp and os.path.exists(lp):
                try:
                    with zipfile.ZipFile(lp, 'r') as z:
                        for n in [f for f in z.namelist() if f.endswith('libflite.so')]:
                            with z.open(n) as s, open(os.path.join(natives_dir, "libflite.so"), "wb") as d: d.write(s.read())
                except Exception: pass
    
    # Hybrid Asset Sharing Architecture (Offline Isolate Assets)
    _asset_mode_isolated = bool(args.isolate_assets)
    if args.isolate_assets and not args.dry_run:
        print(f"[ 📦 ] \033[1;94mIsolating assets and libraries to instance (Delta-Copy)...\033[0m")
        inst_assets = os.path.join(INST_DIR, "assets")
        inst_libraries = os.path.join(INST_DIR, "libraries")
        
        def delta_copy(src_dir, dst_dir):
            if not os.path.exists(src_dir): return
            os.makedirs(dst_dir, exist_ok=True)
            for root, _, files in os.walk(src_dir):
                rel_path = os.path.relpath(root, src_dir)
                target_dir = os.path.join(dst_dir, rel_path) if rel_path != '.' else dst_dir
                os.makedirs(target_dir, exist_ok=True)
                for file in files:
                    s_file = os.path.join(root, file)
                    d_file = os.path.join(target_dir, file)
                    needs_copy = False
                    if not os.path.exists(d_file):
                        needs_copy = True
                    elif os.path.getsize(s_file) != os.path.getsize(d_file):
                        needs_copy = True
                    else:
                        # Phase 3 engine: size+mtime fast-path, full-hash fallback (never skips wrongly)
                        if not files_identical(s_file, d_file):
                            needs_copy = True
                    if needs_copy:
                        shutil.copy2(s_file, d_file)
                        
        delta_copy(os.path.join(MC_DIR, "assets"), inst_assets)
        delta_copy(os.path.join(MC_DIR, "libraries"), inst_libraries)
        
        # Override classpath to point to the isolated instance libraries
        lib_root = os.path.join(MC_DIR, "libraries")
        cp_paths = [inst_libraries + p[len(lib_root):] if p.startswith(lib_root) else p for p in cp_paths]
        
        with open(os.path.join(INST_DIR, ".asset_mode"), "w", encoding="utf-8") as f:
            f.write("isolated")
    else:
        # Check if instance was previously isolated
        asset_mode_file = os.path.join(INST_DIR, ".asset_mode")
        if os.path.exists(asset_mode_file):
            with open(asset_mode_file, "r", encoding="utf-8") as f:
                if f.read().strip() == "isolated":
                    _asset_mode_isolated = True
                    lib_root = os.path.join(MC_DIR, "libraries")
                    inst_libraries = os.path.join(INST_DIR, "libraries")
                    cp_paths = [inst_libraries + p[len(lib_root):] if p.startswith(lib_root) else p for p in cp_paths]
        elif not args.dry_run:
            # SUG-114: first-launch initialization marker; never write it during --dry-run
            with open(asset_mode_file, "w", encoding="utf-8") as f:
                f.write("shared")

    # Write integrity marker after all extraction and isolation is complete
    if not args.dry_run and not args.offline and not (os.path.exists(integrity_marker) and not args.recheck):
        with open(integrity_marker, 'w', encoding='utf-8') as f: f.write("OK")

    # Exit the program if the user only wanted to download game files.
    if args.game_download_only:
        # Pre-download Java if not using system Java (respects --java flag)
        if args.java == "java" and not args.system_java: get_adoptium_java(v_mjvn)
        
        print(f"\n[ ✅ ] \033[1;97mGame {VERSION} Downloaded Successfully\033[0m")
        print(f"[ ✅ ] \033[1;92m{platform_os} library included...\033[0m")
        if is_windows:
            print(f"\n[ 🔧 ] \033[1;97mFabric Installation Path (.exe):\033[0m \033[1;96m{INST_DIR}\033[0m")
        else:
            print(f"\n[ 🔧 ] \033[1;97mFabric Installation Path (.jar):\033[0m \033[1;96m{INST_DIR}\033[0m")
        print(f"\n[ 👋 ] \033[1;97mBYE...\033[0m\n")
        sys.exit(0)
    
    # THE Local Authentication EXECUTION
    def build_cmd():
        def get_mb_value(size_str):
            # Normalize any memory string input into a standard integer of Megabytes.
            try:
                size_str = size_str.upper().strip()
                if size_str.endswith('G'): result = int(float(size_str[:-1]) * 1024) # Handle Gigabyte style input
                elif size_str.endswith('M'): result = int(float(size_str[:-1]))  # Handle Megabyte style input
                else: result = int(size_str)
                return max(256, result)
            except (ValueError, TypeError): return 2048 # Safe 2GB fallback on invalid input
    
        max_mb = get_mb_value(MEMORY)
        min_mb = min(1024, max_mb)
    
        # v_mjvn already computed globally after resolve_version_json()
        
        # Phase 5 Auto-Java Execution
        FINAL_JAVA_BIN = resolve_final_java(v_mjvn)
        
        # Base JVM Command
        cmd = [FINAL_JAVA_BIN, f"-Xmx{max_mb}M", f"-Xms{min_mb}M"]
    
        # GC & PERFORMANCE OPTIMIZATIONS
        
        cmd.extend([
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+AlwaysPreTouch",
            "-XX:+DisableExplicitGC"
        ])
        
        if v_mjvn >= 21:
            cmd.extend(["-XX:+UseZGC", "-XX:+UseStringDeduplication"])
            if v_mjvn < 24: cmd.append("-XX:+ZGenerational")
        elif v_mjvn >= 17:
            cmd.extend(["-XX:+UseZGC"])
        else:
            cmd.extend(["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=50", "-XX:G1NewSizePercent=20", "-XX:G1ReservePercent=20", "-XX:MaxTenuringThreshold=1", "-XX:+UseStringDeduplication"])
    
        # AUTOMATIC HUGE PAGES / LARGE PAGES DETECTION
        huge_pages_confirm = False
        intentionally_disabled_huge_pages = False
        
        if is_windows:
            if has_large_pages_privilege():
                if not args.disable_large_pages:
                    cmd.extend(["-XX:+UseLargePages"])
                    huge_pages_confirm = True
                else:
                    intentionally_disabled_huge_pages = True
        elif not is_mac:
            # Check if Linux and if THP is enabled/supported
            thp_path = "/sys/kernel/mm/transparent_hugepage/enabled"
            use_huge_pages = False
            if os.path.exists(thp_path):
                with open(thp_path, 'r', encoding='utf-8') as f:
                    status = f.read()
                    if "[always]" in status or "[madvise]" in status:
                        use_huge_pages = True
            if not args.disable_huge_pages:
                if use_huge_pages:
                    cmd.extend(["-XX:+UseLargePages"])
                    huge_pages_confirm = True
            else:
                intentionally_disabled_huge_pages = True
        
        # Appending remaining flags
        if not args.old_compatibility and v_mjvn >= 17: cmd.append("--enable-native-access=ALL-UNNAMED")
        
        ## Override OpenAL behaviour (POSIX)
        if not is_windows:
            if args.force_disable_openal:
                if os.path.exists(natives_dir):
                    for f in os.listdir(natives_dir):
                        if "openal" in f.lower():
                            try: os.remove(os.path.join(natives_dir, f))
                            except Exception: pass
            elif args.force_openal:
                for _openal_path in ("/usr/lib/libopenal.so.1", "/usr/lib64/libopenal.so.1",
                                     "/usr/local/lib/libopenal.so.1", "/usr/local/lib/libopenal.so"):
                    if os.path.exists(_openal_path):
                        cmd.append(f"-Dorg.lwjgl.openal.libname={os.path.basename(_openal_path)}")
                        break
        
        cmd.extend([
            f"-Djava.library.path={natives_dir}", 
            f"-Djna.library.path={natives_dir}", 
            f"-D{_GAME_KEY}.launcher.brand=NuxCraft-PyCher({launcher_version})",
            f"-D{_GAME_KEY}.launcher.version={launcher_version}"
            ])
        
        if JVM_ARGS.strip():
            try:
                cmd.extend(shlex.split(JVM_ARGS))
            except ValueError:
                cmd.extend(JVM_ARGS.split())
    
        cp_paths[:] = list(dict.fromkeys(cp_paths))
        params = {
            "${auth_player_name}": USERNAME, 
            "${version_name}": VERSION, 
            "${game_directory}": INST_DIR, 
            "${assets_root}": os.path.join(INST_DIR, "assets") if (args.isolate_assets or _asset_mode_isolated) else os.path.join(MC_DIR, "assets"), 
            "${assets_index_name}": a_id, 
            "${auth_uuid}": UUID, 
            "${auth_access_token}": "null", 
            "${user_type}": "mojang", 
            "${version_type}": v_json.get('type', 'release'), 
            "${natives_directory}": natives_dir, 
            "${classpath}": cp_separator.join(cp_paths) # Dynamic Classpath Separator
        }
    
        def parse_arg(arg_str):
            for k, v in params.items(): arg_str = arg_str.replace(k, str(v))
            return arg_str

        main_cls = v_json.get('mainClass')
        if 'arguments' in v_json:
            for arg in v_json['arguments'].get('jvm', []):
                if isinstance(arg, str): cmd.append(parse_arg(arg))
                elif isinstance(arg, dict) and is_allowed(arg.get('rules')):
                    val = arg['value'] if isinstance(arg['value'], list) else [arg['value']]
                    cmd.extend([parse_arg(v) for v in val])
            if main_cls: cmd.append(main_cls)
            for arg in v_json['arguments'].get('game', []):
                if isinstance(arg, str):
                    cmd.append(parse_arg(arg))
                elif isinstance(arg, dict) and is_allowed(arg.get('rules')):
                    val = arg['value'] if isinstance(arg['value'], list) else [arg['value']]
                    cmd.extend([parse_arg(v) for v in val])
        else:
            cmd.append("-cp")
            cmd.append(cp_separator.join(cp_paths))
            if main_cls: cmd.append(main_cls)
            if _GAME_ARGS_KEY in v_json:
                leg_str = v_json[_GAME_ARGS_KEY]
                for k, v in params.items(): leg_str = leg_str.replace(k, v)
                try:
                    cmd.extend(shlex.split(leg_str))
                except ValueError:
                    cmd.extend(leg_str.split())
    
        if GAME_ARGS.strip():
            try:
                cmd.extend(shlex.split(GAME_ARGS))
            except ValueError:
                cmd.extend(GAME_ARGS.split())
        if args.fullscreen: cmd.append('--fullscreen')
        if DEMO_MODE: cmd.append('--demo')
        return cmd, huge_pages_confirm, intentionally_disabled_huge_pages
    
    final_cmd, huge_pages_active, intentionally_disabled_huge_pages = build_cmd()
    
    # v_mjvn already computed globally after resolve_version_json()
    
    if not is_mac:
        page_term = "Large Pages" if is_windows else "Transparent Huge Pages (THP)"
        if huge_pages_active:
            print(f"\n[ ✅ ] \033[1;92m{page_term} enabled\033[0m")
        else:
            if intentionally_disabled_huge_pages:
                print(f"\n[ ℹ️ ] \033[1;96m{page_term}\033[1;97m has been disabled by the user\033[0m.")
            else:
                extra_msg = "(\033[1;91mRequires Admin/GPO\033[1;97m)" if is_windows else "not detected or disabled."
                print(f"\n[ ℹ️ ] \033[1;97mNOTE: \033[1;96m{page_term}\033[1;97m {extra_msg}\033[0m\n", 
                      f"      \033[1;97mFor optimal performance, consider enabling \033[1;96m{page_term}\033[1;97m on your system (\033[1;96mOptional\033[1;97m).\033[0m\n" if is_windows else f"      \033[1;97mFor optimal performance, consider enabling \033[1;96m{page_term}\033[1;97m on your system (\033[1;96mOptional\033[1;97m).\033[0m")
    
    print(f"\n[ 👍 ] Finalizing... \n", 
          f"        🎮 \033[1;97mGame Version:\033[0m \033[1;92m{VERSION}\033[0m\n", 
          f"        👩 \033[1;97mPlayer Name:\033[0m \033[1;92m{USERNAME}\033[0m\n", 
          f"        🎚️ \033[1;97mMax Allocated RAM:\033[0m \033[1;92m{MEMORY}\033[0m\n", 
          f"        📈 \033[1;97mMax Thread Count:\033[0m \033[1;92m{MAX_THREAD_COUNT}\033[0m\n", 
          f"        ☕ \033[1;97mRequired major Java Version:\033[0m \033[1;92m{v_mjvn}\033[0m\n"
          )
    
    if DEMO_MODE: print(f"\n    [ ⚠️ ] \033[1;93mWARNING:\033[0m DEMO MODE enabled...\n", 
                        f"    \033[1;97mYES, YOU did it... INTENTIONALLY!!!\033[0m\n", 
                        f"    Have a nice \033[1;97m1 Hour 40 Minutes\033[0m DEMO!!!\n"
                        )
    
    # Save version to cache for TUI highlighting next time
    if not args.dry_run:
        try:
            os.makedirs(os.path.join(MC_DIR, "cache"), exist_ok=True)
            with open(os.path.join(MC_DIR, "cache/last_version.txt"), 'w', encoding='utf-8') as f: f.write(VERSION)
        except Exception: pass
    
    if args.print_cmd:
        # SUG-008: print the raw final command (sterile — implies --dry-run)
        print(f"{' '.join(final_cmd)}")
        sys.exit(0)
    if args.dry_run:
        print(f"\n[ 🧪 ] \033[1;96mDry-run complete.\033[0m \033[1;97mConstructed JVM command:\033[0m\n")
        print(f"  \033[1;92m{' '.join(final_cmd)}\033[0m\n")
        print(f"[ ℹ️ ] \033[1;97mTotal classpath entries:\033[0m \033[1;92m{len([x for x in final_cmd if os.sep in x and x.endswith('.jar')])}\033[0m")
        print(f"[ ℹ️ ] \033[1;97mTotal JVM args:\033[0m \033[1;92m{len(final_cmd)}\033[0m")
        print(f"\n[ 👋 ] \033[1;97mNo files were downloaded. No game was launched. BYE...\033[0m\n")
        sys.exit(0)
    
    # ---- PHASE 5: auto saves-snapshot before launch (Q5-C) ----
    if (AUTO_SNAPSHOT_SAVES and INSTANCE_NAME and not args.dry_run
            and not args.game_download_only and not args.check_java):
        try:
            _sp = create_snapshot(INSTANCE_NAME, 'saves')
            logger.debug(f"Auto snapshot: {_sp}")
        except Exception as _sse:
            logger.warning(f"Auto snapshot skipped: {_sse}")
    
    # SUG-006: verify the resolved Java binary exists before detaching (emits EXIT_JAVA)
    _java_bin = final_cmd[0] if final_cmd else "java"
    if shutil.which(_java_bin) is None and not os.path.isfile(_java_bin):
        logger.error(f"[ ❌ ] Java binary not found: {_java_bin}")
        sys.exit(EXIT_JAVA)

    os.makedirs(os.path.join(INST_DIR, "logs"), exist_ok=True)
    with open(os.path.join(INST_DIR, "logs/latest_launch.log"), "wb") as f:
        f.write(f"    (PLATFORM: {platform_os}) COMMAND EXECUTED:\n\n{' '.join(final_cmd)}\n\n".encode('utf-8'))
        f.write(("#" * 25 + " GAME OUTPUT START " + "#" * 25 + "\n\n").encode('utf-8'))
        f.flush()
        
        # Detach and exit (Fire-and-Forget)
        kwargs = {
            "cwd": INST_DIR,
            "stdout": f,
            "stderr": f
        }
        if is_windows:
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            kwargs["close_fds"] = False  # Preserve handle inheritance for log file
        else:
            kwargs["start_new_session"] = True # POSIX detach
            
        subprocess.Popen(final_cmd, **kwargs)
        
        print("[ ✅ ] \033[1;97mGame launch started.\033[0m")
        print("[ ⏰ ] \033[1;97mPlease, be patient...\033[0m\n")
        sys.exit(0)

except KeyboardInterrupt:
    print("\n\n[ 💀 ] \033[1;91mShutdown requested by user. BYE...\033[0m\n")
    sys.exit(1)
except Exception as e:
    # SMELL-102 (partial): surface unexpected errors cleanly instead of a raw
    # traceback. Full functional decomposition of the mega-try remains deferred.
    try:
        logger.error(f"[ 💀 ] Unexpected launcher error: {e.__class__.__name__}: {e}")
        logger.error("Please report this bug and include the launcher.log file.")
    except Exception:
        print(f"[ 💀 ] Unexpected launcher error: {e.__class__.__name__}: {e}")
        print("Please report this bug and include the launcher.log file.")
    sys.exit(1)
