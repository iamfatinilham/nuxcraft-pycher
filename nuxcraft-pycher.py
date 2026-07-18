#!/usr/bin/env python3

import os, json, subprocess, shutil, zipfile, sys, argparse, hashlib, time, uuid, multiprocessing, base64, re, datetime
from concurrent.futures import ThreadPoolExecutor

# Future-proof: Change this single constant if Mojang ever switches hash algorithms
HASH_ALGO = hashlib.sha1

## ⚠️ Disclaimer: This project is for educational, research and testing purposes only.

############################
##### LAUNCHER VERSION #####
############################
launcher_version = "0.8"
############################

# Force UTF-8 Encoding globally to handle emojis across all OS configurations
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass

# OS Detection
is_windows = sys.platform == "win32"
is_mac = sys.platform == "darwin"

if is_windows:
    import msvcrt, ctypes
    platform_os = "windows"
    cp_separator = ";"
    os.system('') # Enable ANSI escape processing
    ansi_clear = False
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                ansi_clear = True
        else:
            ansi_clear = sys.getwindowsversion().build >= 10586
    except: pass
else:
    import tty, termios
    platform_os = "osx" if is_mac else "linux"
    cp_separator = ":"
    ansi_clear = True

def has_large_pages_privilege():
    # Checks if the process actually has SeLockMemoryPrivilege enabled (required for -XX:+UseLargePages)
    if not is_windows: return False
    SE_LOCK_MEMORY_NAME = "SeLockMemoryPrivilege"
    TOKEN_QUERY = 0x0008
    try:
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
        ps.Attributes = 0
        
        result = ctypes.c_long()
        if ctypes.windll.advapi32.PrivilegeCheck(token, ctypes.byref(ps), ctypes.byref(result)):
            ctypes.windll.kernel32.CloseHandle(token)
            return result.value != 0
            
        ctypes.windll.kernel32.CloseHandle(token)
        return False
    except: return False    

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
    
    default_max_threads = multiprocessing.cpu_count()
    
    parser = argparse.ArgumentParser(description=f"  NuxCraft-PyCher ({platform_os}) Version: {launcher_version}")
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
    parser.add_argument("-t", "--threads", type=int, dest="threads", metavar="NUMBER", default=default_max_threads, help=f"  Allocate max number of threads (e.g. 4) | Default: {default_max_threads}")
    parser.add_argument("--last", "--offline", action="store_true", dest="offline", help="  Launch last version instantly")
    parser.add_argument("--jvm-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for JVM when launching game")
    parser.add_argument("--game-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for the game when launching game")
    parser.add_argument("--download-only", action="store_true", dest="game_download_only", help="  Only Download game files.")
    parser.add_argument("--cj", "--check-java", action="store_true", dest="check_java", help="  Check required Java version and exit")
    parser.add_argument("--demo", "--demo-mode", action="store_true", dest="demo_mode", help="  Launch the game in demo mode")
    parser.add_argument("--auto-install", action="store_true", dest="auto_install", help="  Automatically install missing dependencies")
    parser.add_argument("--isolate-assets", action="store_true", dest="isolate_assets", help="  Copy assets and libraries into the instance folder for standalone portability")
    parser.add_argument("--system-java", action="store_true", dest="system_java", help="  Force use of system java binary instead of auto-downloading Adoptium")
    
    # Platform-specific flags (exposed everywhere but may no-op)
    parser.add_argument("--no-openal", action="store_true", dest="force_disable_openal", help="  Force disable use of openal if possible (Linux)")
    parser.add_argument("--openal", action="store_true", dest="force_openal", help="  Use of openal if possible (Linux)")
    parser.add_argument("--dhp", "--disable-huge-pages", action="store_true", dest="disable_huge_pages", help="  Disable Transparent Huge Pages (Linux)")
    parser.add_argument("--dlp", "--disable-large-pages", action="store_true", dest="disable_large_pages", help="  Disable Large Pages (Windows)")
    
    args = parser.parse_args()
    
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
            ans = input("    Do you want to install them now? [Y/n]: ").strip().lower()
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
    
    if args.threads <= 0:
        print(f"[ ❌ ] \033[1;91mError:\033[0m Invalid thread count specified: {args.threads}. Must be a positive integer.")
        sys.exit(1)
    
    args.threads = min(args.threads, multiprocessing.cpu_count())
    
    # Useful vars (all of them generated on the fly) [better not to edit them]
    USERNAME = args.player
    UUID = generate_offline_uuid(USERNAME)
    MC_DIR = os.path.abspath(args.game_dir)
    MEMORY = args.memory
    MAX_THREAD_COUNT = args.threads
    JVM_ARGS = args.jvm_flags
    GAME_ARGS = args.game_flags
    DEMO_MODE = args.demo_mode
    
    # Simple thing... You know but do not say...
    b64d = lambda dta: base64.b64decode(dta).decode('utf-8')
    
    for folder in ['instances', 'libraries', 'assets/indexes', 'assets/objects', 'resources', 'cache', 'logs', 'backups']:
        os.makedirs(os.path.join(MC_DIR, folder), exist_ok=True)
    
    # INSTANCE MANAGEMENT UTILITIES
    INSTANCE_DIRS = ['mods', 'config', 'saves', 'resourcepacks', 'shaderpacks', 'screenshots', 'logs']
    
    def sanitize_instance_name(name):
        # Sanitize instance name by removing illegal OS folder characters.
        sanitized = re.sub(r'[\\/:*?"<>|]', '', name).strip()
        if not sanitized: sanitized = "instance"
        return sanitized
    
    def list_instances():
        # Lists all valid instance directories inside .game/instances/.
        instances_root = os.path.join(MC_DIR, "instances")
        if not os.path.exists(instances_root): return []
        return sorted([d for d in os.listdir(instances_root) if os.path.isdir(os.path.join(instances_root, d))])
    
    # Phase 4 Backup System Initialization
    config_path = os.path.join(MC_DIR, "nuxcraft_config.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"max_backups": 20}, f, indent=4)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
        max_backups = cfg.get("max_backups", 20)

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

    # UTILITIES
    session = requests.Session()
    session.headers.update({"User-Agent": f"NuxCraft-PyCher/{launcher_version} ({platform_os})"})
    
    def get_adoptium_java(major_version):
        # Downloads and extracts the required Java version from Adoptium API into .game/runtimes/.
        import platform, zipfile, tarfile
        os_map = {"windows": "windows", "darwin": "mac", "linux": "linux"}
        arch_map = {"amd64": "x64", "x86_64": "x64", "arm64": "aarch64", "aarch64": "aarch64", "arm": "arm", "x86": "x86"}
        ad_os = os_map.get(platform.system().lower(), "linux")
        ad_arch = arch_map.get(platform.machine().lower(), "x64")
        
        api_url = f"https://api.adoptium.net/v3/assets/latest/{major_version}/hotspot?os={ad_os}&architecture={ad_arch}&image_type=jre"
        try:
            r = session.get(api_url, timeout=10)
            if r.status_code != 200:
                print(f"[ ! ] \033[1;91mAdoptium API returned {r.status_code} for Java {major_version}\033[0m")
                return "java" # fallback
            data = r.json()
            if not data: return "java"
            pkg = data[0]['binary']['package']
            dl_url, pkg_name = pkg['link'], pkg['name']
            
            runtime_dir = os.path.join(MC_DIR, "runtimes", f"{ad_os}-{ad_arch}", f"java-{major_version}")
            os.makedirs(runtime_dir, exist_ok=True)
            
            # Find the actual java executable inside
            exe_ext = ".exe" if ad_os == "windows" else ""
            for root, dirs, files in os.walk(runtime_dir):
                if f"java{exe_ext}" in files:
                    return os.path.join(root, f"java{exe_ext}")
            
            pkg_path = os.path.join(runtime_dir, pkg_name)
            if not os.path.exists(pkg_path):
                print(f"[ ☕ ] \033[1;94mDownloading Java {major_version} ({ad_os}-{ad_arch})...\033[0m")
                get(dl_url, pkg_path)
            
            if not os.path.exists(pkg_path):
                print(f"[ ! ] \033[1;93mWarning: Could not download Java (Offline Mode). Falling back to system java.\033[0m")
                return "java"
                
            print(f"[ 📦 ] \033[1;94mExtracting Java Runtime...\033[0m")
            if pkg_name.endswith('.zip'):
                with zipfile.ZipFile(pkg_path, 'r') as z: z.extractall(runtime_dir)
            elif pkg_name.endswith('.tar.gz'):
                with tarfile.open(pkg_path, 'r:gz') as t: t.extractall(runtime_dir)
            os.remove(pkg_path)
            
            for root, dirs, files in os.walk(runtime_dir):
                if f"java{exe_ext}" in files:
                    java_bin = os.path.join(root, f"java{exe_ext}")
                    if ad_os != "windows": os.chmod(java_bin, 0o755)
                    return java_bin
            return "java"
        except Exception as e:
            print(f"[ ! ] \033[1;91mFailed to auto-download Java: {e}\033[0m")
            return "java"
    
    def get(url, path, expected_hash=None, silent=False):
        if args.offline: return
        def verify():
            if not expected_hash or not os.path.exists(path): return False
            if os.path.getsize(path) == 0: return False # Treat empty files as invalid
            sha1 = HASH_ALGO()
            with open(path, 'rb') as f:
                while chunk := f.read(8192): sha1.update(chunk)
            return sha1.hexdigest() == expected_hash
    
        if verify(): return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with session.get(url, timeout=15, stream=True) as r:
                r.raise_for_status()
                total = int(r.headers.get('content-length', 0))
                with open(path, 'wb') as f, tqdm(total=total, unit='B', unit_scale=True, 
                    unit_divisor=1024, desc=f"  [ ☕ ] \033[1;94mSyncing {os.path.basename(path)}\033[0m", disable=silent, bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}\033[0m \033[1;97m[{rate_fmt}]\033[0m  ") as bar:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk: f.write(chunk); bar.update(len(chunk))
        except Exception as e:
            if not silent: print(f"[ ! ] \033[1;91mError downloading {url}:\033[0m {e}")
    
    def is_allowed(rules):
        if not rules: return True
        allowed = False
        for r in rules:
            if 'os' in r:
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
    
    selected_instance = None
    if not INSTANCE_NAME:
        # INSTANCE SELECTION TUI
        def interactive_instance_select(instances, last_instance=""):
            # Arrow-key TUI for selecting an existing instance or creating a new one.
            def get_linux_key():
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    ch = sys.stdin.read(1)
                    if ch == '\x1b':
                        ch += sys.stdin.read(2)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                return ch
            
            if not sys.stdout.isatty(): return None
            
            # Build options: existing instances + "Create New Instance"
            options = []
            for inst in instances:
                ver = get_instance_version(inst) or "empty"
                options.append({"id": inst, "display": f"{inst} ({ver})", "type": "instance"})
            options.append({"id": "__create_new__", "display": "\033[1;93m+ Create New Instance\033[0m", "type": "create"})
            options.append({"id": "__create_fabric__", "display": "\033[1;95m+ Create Fabric Instance\033[0m", "type": "create_fabric"})
            
            total = len(options)
            curr = 0
            
            # Jump to last played instance if exists
            if last_instance:
                for i, opt in enumerate(options):
                    if opt['id'] == last_instance:
                        curr = i
                        break
            
            while True:
                try:
                    term_height = os.get_terminal_size().lines
                    window_size = max(5, term_height - 8)
                except:
                    window_size = 15
                
                # Build the entire frame in memory
                if ansi_clear:
                    buf = ["\033[H\033[J\n\033[1;96m------ Select Instance ------\033[0m\n"]
                else:
                    os.system('cls')
                    buf = ["\n\033[1;96m------ Select Instance ------\033[0m\n"]
                
                buf.append(f"\033[1;97mNavigate: \033[1;96mArrows\033[1;97m ( \033[1;96m↑\033[1;97m and \033[1;96m↓\033[1;97m ) | \033[1;97mSelect: \033[1;96mEnter\033[1;97m\n\n")
                
                start = max(0, min(curr - window_size // 2, total - window_size))
                end = min(start + window_size, total)
                
                for i in range(start, end):
                    opt = options[i]
                    is_selected = (i == curr)
                    is_last = (opt['id'] == last_instance)
                    
                    sel_marker = "  [ \033[1;96mX\033[0m ]\033[1;96m " if is_selected else "  [   ]\033[1;97m "
                    line = f"{sel_marker}{opt['display']}\033[0m"
                    if is_last and opt['type'] == "instance":
                        line += "  \033[1;91m<-- (Last Played)\033[0m"
                    buf.append(line + "\n")
                
                buf.append(f"\n  [ \033[1;94m{curr + 1}\033[0m / \033[1;94m{total}\033[0m ]\n")
                
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
                
                # INPUT HANDLING
                if is_windows:
                    key = msvcrt.getch()
                    if key == b'\xe0':
                        key = msvcrt.getch()
                        if key == b'H': curr = max(0, curr - 1)
                        elif key == b'P': curr = min(total - 1, curr + 1)
                    elif key in (b'\r', b'\n'):
                        return options[curr]
                    elif key == b'\x1b':  # Esc
                        return None
                else:
                    key = get_linux_key()
                    if key == '\x1b[A': curr = max(0, curr - 1)
                    elif key == '\x1b[B': curr = min(total - 1, curr + 1)
                    elif key in ('\r', '\n'): return options[curr]
                    elif key == '\x1b':  # Esc (raw, not arrow)
                        return None

        existing_instances = list_instances()
        last_saved_instance = ""
        if not args.check_java and os.path.exists(last_inst_file):
            with open(last_inst_file, 'r', encoding='utf-8') as f: last_saved_instance = f.read().strip()
        
        selected_instance = interactive_instance_select(existing_instances, last_saved_instance)
        
        if selected_instance is None:
            # FALLBACK for Instance Selection
            while True:
                print("\n\033[1;96m  ---- INSTANCE LIST ----\033[0m")
                for i, inst in enumerate(existing_instances):
                    ver = get_instance_version(inst) or "empty"
                    marker = " \033[1;91m<-- [LAST PLAYED]\033[0m" if inst == last_saved_instance else ""
                    print(f"    \033[1;96m{i+1}\033[0m. \033[1;97m{inst}\033[0m ({ver}){marker}")
                print(f"    \033[1;96m{len(existing_instances)+1}\033[0m. \033[1;93m+ Create New Instance\033[0m")
                print(f"    \033[1;96m{len(existing_instances)+2}\033[0m. \033[1;95m+ Create Fabric Instance\033[0m")
                
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
                except:
                    pass
        
        if selected_instance['type'] == 'instance':
            # Existing instance selected
            INSTANCE_NAME = selected_instance['id']
            VERSION = get_instance_version(INSTANCE_NAME)
            if not VERSION:
                print(f"\n[ ❌ ] \033[1;91mInstance '{INSTANCE_NAME}' has no version installed.\033[0m")
                sys.exit(1)
    
    is_creating_fabric = False
    if not VERSION:
        if selected_instance and selected_instance['type'] == 'create_fabric':
            is_creating_fabric = True
            print("\n[ 🔧 ] \033[1;95mFabric Installation Flow Initiated.\033[0m")
        try:
            if args.refresh or not os.path.exists(manifest_cache):
                manifest_json_remote_source1 = b64d('aHR0cHM6Ly9sYXVuY2hlcm1ldGEubW9qYW5nLmNvbS9tYy9nYW1lL3ZlcnNpb25fbWFuaWZlc3QuanNvbg==')
                manifest_json_remote_source2 = b64d('aHR0cHM6Ly9waXN0b24tbWV0YS5tb2phbmcuY29tL21jL2dhbWUvdmVyc2lvbl9tYW5pZmVzdC5qc29u')
                try:
                    r = session.get(manifest_json_remote_source1, timeout=15)
                    r.raise_for_status()
                except requests.exceptions.RequestException:
                    print(f"[ ❌ ] Cannot fetch version list from {manifest_json_remote_source1}")
                    print(f"     Trying {manifest_json_remote_source2}")
                    r = session.get(manifest_json_remote_source2, timeout=15)
                
                manifest = r.json()
                with open(manifest_cache, 'w', encoding='utf-8') as f: json.dump(manifest, f)
            else:
                with open(manifest_cache, 'r', encoding='utf-8') as f: manifest = json.load(f)
        except:
            if os.path.exists(manifest_cache):
                with open(manifest_cache, 'r', encoding='utf-8') as f: manifest = json.load(f)
            else:
                print("[ ❌ ] Failed to fetch version manifest and no cache available. Check your internet connection.")
                sys.exit(1)
    
        v_pool = [v for v in manifest['versions'] if v['type'] in (['snapshot'] if args.snapshots else (['old_beta', 'old_alpha'] if args.beta else ['release']))]
        last_saved = ""
    
        warning_msg = ""
        if last_saved and not any(v['id'] == last_saved for v in v_pool):
            warning_msg = f"[ ⚠️ ] \033[1;93mWARNING: Last played version '{last_saved}' is hidden by current filters.\033[0m"
    
        # Version menu
        # Interactive Menu setup
        def interactive_select(options, last_saved="", warning_msg=""):
            def get_linux_key():
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    ch = sys.stdin.read(1)
                    if ch == '\x1b':
                        ch += sys.stdin.read(2)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                return ch
                
            # Dynamic arrow-key menu that scales with terminal height.
            if not sys.stdout.isatty(): return None
    
            total = len(options)
            curr = 0
    
            # FIND THE LAST USED VERSION INDEX
            if last_saved:
                for i, v in enumerate(options):
                    if v['id'] == last_saved:
                        curr = i
                        break
        
            while True:
                try:
                    term_height = os.get_terminal_size().lines
                    # Reserve 6 lines for header/footer
                    window_size = max(5, term_height - 8)
                except:
                    window_size = 15
    
                # Build the entire frame in memory to prevent screen tearing/flickering
                if ansi_clear:
                    buf = ["\033[H\033[J\n\033[1;96m------ Choose Game version ------\033[0m\n"]
                else:
                    os.system('cls')
                    buf = ["\n\033[1;96m------ Choose Game version ------\033[0m\n"]
                
                if warning_msg: buf.append(f"  {warning_msg}\n")
                buf.append(f"\033[1;97mNavigate: \033[1;96mArrows\033[1;97m ( \033[1;96m↑\033[1;97m and \033[1;96m↓\033[1;97m ) | \033[1;97mSelect: \033[1;96mEnter\033[1;97m | \033[1;97mBack: \033[1;96mEsc\033[1;97m | \033[1;97m{'Use less / ' if not is_windows else ''}Print Mode: \033[1;96mQ\033[1;97m\n\n")
    
                # CALCULATE WINDOW SLICE
                # Keep the selection 'curr' within the visible window
                start = max(0, min(curr - window_size // 2, total - window_size))
                end = min(start + window_size, total)
    
                for i in range(start, end):
                    v = options[i]
                    # Use different symbols for selected vs last-used
                    is_selected = (i == curr)
                    is_last = (v['id'] == last_saved)
    
                    sel_marker = "  [ \033[1;96mX\033[0m ]\033[1;96m " if is_selected else "  [   ]\033[1;97m "
    
                    line = f"{sel_marker}{v['id']}\033[0m (\033[1;93m{v['type']}\033[0m)\033[0m"
                    if is_last:
                        line += "  \033[1;91m<-- (Last Selected)\033[0m"
    
                    buf.append(line + "\n")
    
                buf.append(f"\n  [ \033[1;94m{curr + 1}\033[0m / \033[1;94m{total}\033[0m ] | Page: \033[1;94m{start+1}\033[0m-\033[1;94m{end}\033[0m\n")
                
                # Flush the entire frame instantly
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
    
                # INPUT HANDLING
                if is_windows:
                    key = msvcrt.getch()
                    if key == b'\xe0': 
                        key = msvcrt.getch()
                        if key == b'H': curr = max(0, curr - 1)
                        elif key == b'P': curr = min(total - 1, curr + 1)
                    elif key in (b'\r', b'\n'):
                        return options[curr]
                    elif key == b'\x1b':  # Esc = go back
                        return "__back__"
                    elif key.lower() == b'q':
                        os.system('cls')
                        return None
                else:
                    key = get_linux_key()
                    if key == '\x1b[A': curr = max(0, curr -1)
                    elif key == '\x1b[B': curr = min(total - 1, curr + 1)
                    elif key in ('\r', '\n'): return options[curr]
                    elif key == '\x1b':  # Esc = go back (raw, not arrow)
                        return "__back__"
                    elif key.lower() == 'q':
                        print("\033[H\033[J", end="")
                        return None

        # VERSION SELECTION LOOP (with Esc-to-go-back to Instance page)
        while True:
            selected_obj = interactive_select(v_pool, last_saved, warning_msg)
            
            if selected_obj == "__back__":
                # Go back to instance selection
                existing_instances = list_instances()
                selected_instance = interactive_instance_select(existing_instances, last_saved_instance)
                if selected_instance is None:
                    print("\n[ 💀 ] \033[1;91mNo instance selected. Exiting...\033[0m\n")
                    sys.exit(0)
                if selected_instance['type'] == 'instance':
                    INSTANCE_NAME = selected_instance['id']
                    VERSION = get_instance_version(INSTANCE_NAME)
                    if not VERSION:
                        print(f"\n[ ❌ ] \033[1;91mInstance '{INSTANCE_NAME}' has no version installed.\033[0m")
                        sys.exit(1)
                    break
                # If they selected "Create New" again, loop back to version select
                continue
            elif selected_obj is None:
                # Fallback to print mode (Q pressed)
                while True:
                    if warning_msg: print(f"\n  {warning_msg}")
                    print("\n\033[1;96m  ---- Game VERSION LIST ----\033[0m")
                    menu = "\n".join([f"    \033[1;96m{i+1}\033[0m. \033[1;97m{v['id']}\033[0m (\033[1;93m{v['type']}\033[0m)" for i, v in enumerate(v_pool)])
                    if not is_windows:
                        try: subprocess.run(["less", "-XR"], input=menu, text=True, check=True)
                        except: print(menu)
                    else:
                        print(menu)
                    
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
                    except: 
                        pass
                break
            else:
                VERSION, V_URL = selected_obj['id'], selected_obj['url']
                break
        
        # INSTANCE NAMING (for new instances via Questionary)
        if not INSTANCE_NAME:
            if ansi_clear: sys.stdout.write("\033[H\033[J")
            else: os.system('cls')
            
            default_name = VERSION
            try:
                inst_name_raw = questionary.text(
                    "Name your instance:",
                    default=default_name,
                ).ask()
            except:
                inst_name_raw = default_name
            
            if not inst_name_raw: inst_name_raw = default_name
            INSTANCE_NAME = sanitize_instance_name(inst_name_raw)
            
            # Avoid overwriting an existing instance
            base_name = INSTANCE_NAME
            counter = 1
            while os.path.exists(os.path.join(MC_DIR, "instances", INSTANCE_NAME)):
                INSTANCE_NAME = f"{base_name}-{counter}"
                counter += 1
            
            create_instance_dirs(INSTANCE_NAME)
            
            # PHASE 5: In-House Fabric Generation
            if is_creating_fabric:
                try:
                    print(f"\n[ 🌐 ] \033[1;95mFetching Fabric Loaders for {VERSION}...\033[0m")
                    r = session.get(f"https://meta.fabricmc.net/v2/versions/loader/{VERSION}", timeout=10)
                    loaders = [L['loader']['version'] for L in r.json()]
                    if not loaders:
                        print(f"[ ❌ ] \033[1;91mNo Fabric loaders found for {VERSION}.\033[0m")
                        sys.exit(1)
                    
                    loader_options = [{"id": l, "display": f"Fabric Loader \033[1;95m{l}\033[0m", "type": "fabric"} for l in loaders]
                    print(f"\n\033[1;96m------ Select Fabric Loader ------\033[0m")
                    sel_loader_obj = interactive_select(loader_options) if sys.stdout.isatty() else {"id": loaders[0]}
                    if not sel_loader_obj or sel_loader_obj == "__back__":
                        print("\n[ 💀 ] \033[1;91mFabric installation cancelled.\033[0m")
                        sys.exit(1)
                    
                    loader_ver = sel_loader_obj['id']
                    fab_version_id = f"fabric-loader-{loader_ver}-{VERSION}"
                    
                    print(f"[ 📦 ] \033[1;94mGenerating Fabric Profile ({fab_version_id})...\033[0m")
                    prof_r = session.get(f"https://meta.fabricmc.net/v2/versions/loader/{VERSION}/{loader_ver}/profile/json", timeout=10)
                    prof_json = prof_r.json()
                    
                    v_dir = os.path.join(MC_DIR, "instances", INSTANCE_NAME, "versions", fab_version_id)
                    os.makedirs(v_dir, exist_ok=True)
                    with open(os.path.join(v_dir, f"{fab_version_id}.json"), 'w', encoding='utf-8') as f:
                        json.dump(prof_json, f, indent=4)
                    
                    VERSION = fab_version_id
                except Exception as e:
                    print(f"\n[ ❌ ] \033[1;91mFabric Meta API failed: {e}\033[0m")
                    print("[ ⚠️ ] Attempting Fallback Headless Installation...")
                    try:
                        try:
                            meta_r = session.get("https://meta.fabricmc.net/v2/versions/installer", timeout=5)
                            installer_url = meta_r.json()[0]['url']
                            with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'w') as f: f.write(installer_url)
                        except:
                            try:
                                import xml.etree.ElementTree as ET
                                xml_r = session.get("https://maven.fabricmc.net/net/fabricmc/fabric-installer/maven-metadata.xml", timeout=5)
                                root = ET.fromstring(xml_r.text)
                                latest_ver = root.find('.//latest').text
                                installer_url = f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/{latest_ver}/fabric-installer-{latest_ver}.jar"
                                with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'w') as f: f.write(installer_url)
                            except:
                                try:
                                    with open(os.path.join(MC_DIR, "cache", "last_fabric_installer_url.txt"), 'r') as f: installer_url = f.read().strip()
                                except:
                                    raise Exception("Could not resolve Fabric installer URL. Both APIs failed and no cache was found.")
                        installer_path = os.path.join(MC_DIR, "cache", "fabric-installer.jar")
                        get(installer_url, installer_path)
                        subprocess.run(["java", "-jar", installer_path, "client", "-dir", os.path.join(MC_DIR, "instances", INSTANCE_NAME), "-mcversion", VERSION, "-noprofile"], check=True)
                        print(f"\n[ ✅ ] \033[1;92mFallback installation complete. Please manually update .primary_version to the generated fabric version name.\033[0m")
                        sys.exit(0)
                    except Exception as fallback_e:
                        print(f"[ ❌ ] \033[1;91mFallback failed: {fallback_e}\033[0m")
                        print("[ 🗑️ ] Cleaning up incomplete instance...")
                        shutil.rmtree(os.path.join(MC_DIR, "instances", INSTANCE_NAME), ignore_errors=True)
                        sys.exit(1)
            
            with open(os.path.join(MC_DIR, "instances", INSTANCE_NAME, ".primary_version"), 'w', encoding='utf-8') as f:
                f.write(VERSION)
            print(f"\n[ ✅ ] \033[1;92mCreated instance:\033[0m \033[1;97m{INSTANCE_NAME}\033[0m")

    
    # Save last played instance
    if not args.check_java:
        with open(last_inst_file, 'w', encoding='utf-8') as f: f.write(INSTANCE_NAME)
    
    # CHECK RUNTIME ASSETS & NATIVES
    INST_DIR = os.path.join(MC_DIR, "instances", INSTANCE_NAME)
    # LOCAL JSON RESOLUTION & INHERITANCE ENGINE
    def resolve_version_json(version_id, v_url=None):
        # Recursively parses version JSONs and resolves inheritsFrom dependencies.
        v_dir = os.path.join(INST_DIR, f"versions/{version_id}")
        os.makedirs(v_dir, exist_ok=True)
        v_path = os.path.join(v_dir, f"{version_id}.json")
        
        # Download if missing (never overwrites instance JSONs, respecting Phase 2 rules)
        if not os.path.exists(v_path):
            if v_url:
                get(v_url, v_path, silent=True)
            else:
                # Find parent in global manifest
                try:
                    _ = manifest
                except NameError:
                    if os.path.exists(manifest_cache):
                        with open(manifest_cache, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)
                    else:
                        print(f"[ ❌ ] Failed to locate parent version '{version_id}' because Mojang manifest cache is missing.")
                        sys.exit(1)
                        
                p_url = next((v['url'] for v in manifest['versions'] if v['id'] == version_id), None)
                if p_url:
                    get(p_url, v_path, silent=True)
                else:
                    print(f"[ ❌ ] Failed to locate parent version '{version_id}' in Mojang manifest.")
                    sys.exit(1)
        
        with open(v_path, 'r', encoding='utf-8') as f:
            v_data = json.load(f)
            
        if 'inheritsFrom' in v_data:
            parent_id = v_data['inheritsFrom']
            parent_data = resolve_version_json(parent_id)
            
            # 1. Merge libraries
            if 'libraries' in v_data:
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
            _mc_args_key = b64d("bWluZWNyYWZ0QXJndW1lbnRz")
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
    
    # Phase 4 Rolling Backup Execution
    target_json = os.path.join(INST_DIR, f"versions/{VERSION}/{VERSION}.json")
    if os.path.exists(target_json):
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
    
    if args.check_java:
        print(f"\n  [ ☕ ] \033[1;97mRequired major Java Version for {VERSION}:\033[0m \033[1;92mJava {v_mjvn}\033[0m")
        if is_windows:
            print(f"  [ 🔧 ] \033[1;97mFabric Installation Path (.exe):\033[0m \033[1;96m{INST_DIR}\033[0m\n")
        else:
            print(f"  [ 🔧 ] \033[1;97mFabric Installation Path (.jar):\033[0m \033[1;96m{INST_DIR}\033[0m\n")
        sys.exit(0)
    
    # Fabric JSONs often use the 'jar' property to point to the parent vanilla JAR
    jar_version = v_json.get('jar', VERSION)
    jar_dir = os.path.join(INST_DIR, f"versions/{jar_version}")
    os.makedirs(jar_dir, exist_ok=True)
    jar_path = os.path.join(jar_dir, f"{jar_version}.jar")
    
    # Only download jar if marker is missing or recheck is enforced
    if (not os.path.exists(integrity_marker) or args.recheck) and not args.offline:
        get(v_json['downloads']['client']['url'], jar_path, v_json['downloads']['client'].get('sha1'))
    
    cp_paths, lib_queue, natives_queue = [jar_path], [], []
    
    # Mapping variables
    natives_dir = os.path.join(v_root, 'natives')
    os.makedirs(natives_dir, exist_ok=True)
    
    # Parse Libraries
    for lib in v_json['libraries']:
        if not is_allowed(lib.get('rules')): continue
        dl = lib.get('downloads', {})
        if 'artifact' in dl:
            lp = os.path.join(MC_DIR, "libraries", dl['artifact']['path'])
            lib_queue.append((dl['artifact']['url'], lp, dl['artifact'].get('sha1')))
            cp_paths.append(lp)
        elif 'name' in lib and 'url' in lib:
            parts = lib['name'].split(':')
            if len(parts) >= 3:
                g, a, v = parts[0], parts[1], parts[2]
                path = f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}.jar"
                lp = os.path.join(MC_DIR, "libraries", path.replace('/', os.sep))
                url = lib['url']
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
    
    a_id = v_json['assetIndex']['id']
    a_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
    asset_q = []
    
    # Prepare asset queue
    if not args.offline:
        if not os.path.exists(integrity_marker) or args.recheck:
            get(v_json['assetIndex']['url'], a_path, v_json['assetIndex'].get('sha1'), silent=True)
        if os.path.exists(a_path):
            with open(a_path, 'r', encoding='utf-8') as f:
                objs = json.load(f).get('objects', {})
                res_link = b64d("aHR0cHM6Ly9yZXNvdXJjZXMuZG93bmxvYWQubWluZWNyYWZ0Lm5ldA==")
                asset_q = [(f"{res_link}/{h[:2]}/{h}", os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}"), h) for h in [d['hash'] for d in objs.values()]]
    
    # INTEGRITY CHECK, RETRY & SUCCESS MARKER
    if args.offline or (os.path.exists(integrity_marker) and not args.recheck):
        print(f"[ ✅ ] \033[1;92mIntegrity marker found.\033[0m \033[1;97mSkipping verification for VERSION:\033[0m \033[1;92m{VERSION}\033[0m")
    else:
        max_retries = 7
        success = False
    
        for attempt in range(max_retries):
            print(f"\n[ \033[1;95m{attempt+1}\033[0m 🎯 ] \033[1;97mDownload/Verification Attempt:\033[0m ( \033[1;95m{attempt+1}\033[0m / \033[1;95m{max_retries}\033[0m )")
    
            # Run Downloads
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                if lib_queue:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), lib_queue), total=len(lib_queue), desc="  [ 🔍 ] \033[1;94mDownloading & Verifying Libs\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} \033[0m files  "))
                if asset_q:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), asset_q), total=len(asset_q), desc="  [ 🔍 ] \033[1;94mDownloading & Verifying Assets\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} \033[0m items  "))
    
            # Final Integrity Check
            missing = []
            for _, path, _ in lib_queue:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
            for _, path, _ in asset_q:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
    
            if not missing:
                print("[ ✅ ] \033[1;92mAll files verified successfully.\033[0m")
                with open(integrity_marker, 'w', encoding='utf-8') as f: f.write("OK")
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
    
    if args.old_compatibility:
        # Sound compatibility fix for old versions
        shutil.copytree(os.path.join(MC_DIR, "assets"), os.path.join(MC_DIR, "resources"), dirs_exist_ok=True)
        asset_index_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
        if os.path.exists(asset_index_path):
            with open(asset_index_path, 'r', encoding='utf-8') as f:
                index_data = json.load(f)
                objects = index_data.get('objects', {})
                
                # tqdm for visual feedback on sound mapping
                for name, info in tqdm(objects.items(), desc="[ 🔊 ] \033[1;94mReconstructing Legacy Sounds\033[0m", bar_format="{desc}: \033[1;92m{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}\033[0m items  "):
                    h = info['hash']
                    src_file = os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}")
                    dst_file = os.path.join(MC_DIR, "resources", name)
                    
                    if os.path.exists(src_file):
                        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                        if not os.path.exists(dst_file):
                            shutil.copy2(src_file, dst_file)
    
    # Extract natives
    if not os.listdir(natives_dir):
        print(f"[ 📂 ] \033[1;97mExtracting Natives...\033[0m ({platform_os})")
        for np in natives_queue:
            if os.path.exists(np):
                try:
                    with zipfile.ZipFile(np, 'r') as z:
                        ext = '.dll' if is_windows else ('.dylib' if is_mac else '.so')
                        for n in [f for f in z.namelist() if f.endswith(ext)]:
                            with z.open(n) as s, open(os.path.join(natives_dir, os.path.basename(n)), "wb") as d: d.write(s.read())
                except: pass
    
    # ATTENTION NEEDED!!! (For linux only) Specifically extract libflite.so from the text2speech library if found
        if platform_os == "linux":
            for lp in cp_paths:
                if "text2speech" in lp and os.path.exists(lp):
                    try:
                        with zipfile.ZipFile(lp, 'r') as z:
                            for n in [f for f in z.namelist() if f.endswith('libflite.so')]:
                                with z.open(n) as s, open(os.path.join(natives_dir, "libflite.so"), "wb") as d: d.write(s.read())
                    except: pass
                    
    # Phase 4: Hybrid Asset Sharing Architecture (Offline Isolate Assets)
    if args.isolate_assets:
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
                        s_hash, d_hash = HASH_ALGO(), HASH_ALGO()
                        with open(s_file, 'rb') as sf:
                            while chunk := sf.read(8192): s_hash.update(chunk)
                        with open(d_file, 'rb') as df:
                            while chunk := df.read(8192): d_hash.update(chunk)
                        if s_hash.hexdigest() != d_hash.hexdigest():
                            needs_copy = True
                    if needs_copy:
                        shutil.copy2(s_file, d_file)
                        
        delta_copy(os.path.join(MC_DIR, "assets"), inst_assets)
        delta_copy(os.path.join(MC_DIR, "libraries"), inst_libraries)
        
        # Override classpath to point to the isolated instance libraries
        cp_paths = [p.replace(os.path.join(MC_DIR, "libraries"), inst_libraries) for p in cp_paths]
        
        with open(os.path.join(INST_DIR, ".asset_mode"), "w", encoding="utf-8") as f:
            f.write("isolated")
    else:
        with open(os.path.join(INST_DIR, ".asset_mode"), "w", encoding="utf-8") as f:
            f.write("shared")

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
                if size_str.endswith('G'): return int(float(size_str[:-1]) * 1024) # Handle Gigabyte style input
                if size_str.endswith('M'): return int(float(size_str[:-1]))  # Handle Megabyte style input
                return int(size_str)
            except (ValueError, IndexError): return 2048 # Safe 2GB fallback on invalid input
    
        max_mb = get_mb_value(MEMORY)
        min_mb = min(1024, max_mb)
    
        # v_mjvn already computed globally after resolve_version_json()
        
        # Phase 5 Auto-Java Execution
        if args.java != "java":
            FINAL_JAVA_BIN = args.java
        else:
            FINAL_JAVA_BIN = "java" if args.system_java else get_adoptium_java(v_mjvn)
        
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
        if not args.old_compatibility: cmd.append("--enable-native-access=ALL-UNNAMED")
        
        ## Override OpenAL behaviour (POSIX)
        if not is_windows:
            if not args.force_disable_openal or args.force_openal:
                # Force use of system OpenAL if available
                if os.path.exists("/usr/lib/libopenal.so.1"):
                    cmd.append("-Dorg.lwjgl.util.NoChecks=true")
                    cmd.append("-Dorg.lwjgl.librarypath=" + natives_dir)
                    cmd.append("-Dnet.java.games.input.librarypath=" + natives_dir)
        
        game_launcher_name_part = b64d("bWluZWNyYWZ0")

        cmd.extend([
            f"-Djava.library.path={natives_dir}", 
            f"-Djna.library.path={natives_dir}", 
            f"-D{game_launcher_name_part}.launcher.brand=NuxCraft-PyCher({launcher_version})"
            ])
        
        if JVM_ARGS.strip(): cmd.extend(JVM_ARGS.split())
    
        params = {
            "${auth_player_name}": USERNAME, 
            "${version_name}": VERSION, 
            "${game_directory}": INST_DIR, 
            "${assets_root}": os.path.join(INST_DIR, "assets") if args.isolate_assets else os.path.join(MC_DIR, "assets"), 
            "${assets_index_name}": a_id, 
            "${auth_uuid}": UUID, 
            "${auth_access_token}": "null", 
            "${user_type}": "mojang", 
            "${version_type}": "release", 
            "${natives_directory}": natives_dir, 
            "${classpath}": cp_separator.join(cp_paths) # Dynamic Classpath Separator
        }
    
        def parse_arg(arg_str):
            for k, v in params.items(): arg_str = arg_str.replace(k, str(v))
            return arg_str

        if 'arguments' in v_json:
            for arg in v_json['arguments'].get('jvm', []):
                if isinstance(arg, str): cmd.append(parse_arg(arg))
                elif isinstance(arg, dict) and is_allowed(arg.get('rules')):
                    val = arg['value'] if isinstance(arg['value'], list) else [arg['value']]
                    cmd.extend([parse_arg(v) for v in val])
            cmd.append(v_json['mainClass'])
            for arg in v_json['arguments'].get('game', []):
                if isinstance(arg, str): cmd.append(parse_arg(arg))
        else:
            cmd.extend(["-cp", cp_separator.join(cp_paths), v_json['mainClass']])
            game_json_arguments = b64d("bWluZWNyYWZ0QXJndW1lbnRz")
            leg_str = v_json[f"{game_json_arguments}"]
            for k, v in params.items(): leg_str = leg_str.replace(k, v)
            cmd.extend(leg_str.split())
    
        if GAME_ARGS.strip(): cmd.extend(GAME_ARGS.split())
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
    
    os.makedirs(os.path.join(INST_DIR, "logs"), exist_ok=True)
    with open(os.path.join(INST_DIR, "logs/latest_launch.log"), "w", encoding='utf-8') as f:
        f.write(f"    (PLATFORM: {platform_os}) COMMAND EXECUTED:\n\n{' '.join(final_cmd)}\n\n")
        f.write("#" * 25 + " GAME OUTPUT START " + "#" * 25 + "\n\n")
        f.flush()
        
        # Detach and exit (Fire-and-Forget)
        kwargs = {
            "cwd": INST_DIR,
            "stdout": f,
            "stderr": f
        }
        if is_windows:
            kwargs["creationflags"] = 0x00000008 | 0x00000200 # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True # POSIX detach
            
        subprocess.Popen(final_cmd, **kwargs)
        
        print("[ ✅ ] \033[1;97mGame launch started.\033[0m")
        print("[ ⏰ ] \033[1;97mPlease, be patient...\033[0m\n")
        sys.exit(0)

except KeyboardInterrupt:
    print("\n\n[ 💀 ] \033[1;91mShutdown requested by user. BYE...\033[0m\n")
    sys.exit(1)
