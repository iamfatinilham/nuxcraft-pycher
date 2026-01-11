#!/usr/bin/env python3

import os, json, requests, subprocess, shutil, zipfile, sys, argparse, hashlib, time, uuid, multiprocessing, tty, termios, base64
from concurrent.futures import ThreadPoolExecutor

## ⚠️ Disclaimer: This project is for educational, research and testing purposes only.

############################
##### LAUNCHER VERSION #####
############################
launcher_version = 0.3
platform_os = "linux" # If for Linux, then the value should always be "linux" (CASE SENSITIVE).
############################

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
        from tqdm import tqdm
    except ImportError:
        print("[ ⚠️️ ] tqdm not found. Installing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"])
        from tqdm import tqdm
    
    default_max_threads = multiprocessing.cpu_count()
    
    parser = argparse.ArgumentParser(description=f"NuxCraft-PyCher ({platform_os}) Version: {launcher_version}")
    parser.add_argument("-f", "--fullscreen", action="store_true", help="  Launch the game in fullscreen mode")
    parser.add_argument("--java", type=str, metavar="PATH(BINARY FULL_PATH)", default="java", help="  Java binary path")
    parser.add_argument("--game-dir", type=str, metavar="PATH(DIRECTORY FULL_PATH)", default=".game", help="  Custom game directory | Default: .game")
    parser.add_argument("-O", "--old", action="store_true", dest="old_compatibility", help="  For old version compatibility")
    parser.add_argument("-s", "--snapshots", action="store_true", help="  Show snapshot releases")
    parser.add_argument("-b", "--beta", action="store_true", help="  Show old beta releases")
    parser.add_argument("-R", "--refresh", action="store_true", dest="refresh", help="  Fetch version list from internet")
    # parser.add_argument("-r", "--recheck", action="store_true", dest="recheck", help="  Recheck Files") ## Future Plan
    parser.add_argument("-p", "--player", type=str, metavar="NAME", default="player", help="  Set player username | Default: player")
    parser.add_argument("-m", "--memory", type=str, dest="memory", metavar="AMOUNT", default="2G", help="  RAM (e.g. 8G) | Default: 4G")
    parser.add_argument("-t", "--threads", type=int, dest="threads", metavar="NUMBER", default=default_max_threads, help=f"  Allocate max number of threads (e.g. 4) | Default: {default_max_threads}")
    parser.add_argument("--last", "--offline", action="store_true", dest="offline", help="  Launch last version instantly")
    parser.add_argument("--jvm-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for JVM when launching game")
    parser.add_argument("--game-flags", type=str, metavar="FLAGS", default=" ", help="  Parse extra flags/arguments for the game when launching game")
    parser.add_argument("--no-openal", action="store_true", dest="force_disable_openal", help="  Force disable use of openal if possible")
    parser.add_argument("--openal", action="store_true", dest="force_openal", help="  Use of openal if possible")
    parser.add_argument("--dhp", "--disable-huge-pages", action="store_true", dest="disable_huge_pages", help="  Disable Huge Pages")
    parser.add_argument("--download-only", action="store_true", dest="game_download_only", help="  Only Download game files.")
    parser.add_argument("--demo", "--demo-mode", action="store_true", dest="demo_mode", help="  Launch the game in demo mode")
    
    args = parser.parse_args()
    
    
    # Check for Linux environment
    if sys.platform != f"{platform_os}":
        print(f"[ ❌ ] Error: This script is designed specifically for {platform_os}.")
        sys.exit(1)
    
    if args.threads <= 0:
        print(f"[ ❌ ] Error: Invalid thread count specified: {args.threads}. Must be a positive integer.")
        sys.exit(1)
    
    args.threads = min(args.threads, multiprocessing.cpu_count())
    
    # Useful vars (all of them generated on the fly) [better not to edit them]
    USERNAME = args.player
    UUID = generate_offline_uuid(USERNAME)
    JAVA_BIN = args.java
    MC_DIR = os.path.abspath(args.game_dir)
    MEMORY = args.memory
    MAX_THREAD_COUNT = args.threads
    JVM_ARGS = args.jvm_flags
    GAME_ARGS = args.game_flags
    DEMO_MODE = args.demo_mode
    
    # Simple thing... You know but do not say...
    b64d = lambda dta: base64.b64decode(dta).decode('utf-8')
    
    for folder in ['versions', 'libraries', 'assets/indexes', 'assets/objects', 'resources', 'cache', 'logs']:
        os.makedirs(os.path.join(MC_DIR, folder), exist_ok=True)
    
    # UTILITIES
    session = requests.Session()
    session.headers.update({"User-Agent": f"NuxCraft-PyCher/{launcher_version} ({platform_os})"})
    
    def get(url, path, expected_hash=None, silent=False):
        if args.offline: return
        def verify():
            if not expected_hash or not os.path.exists(path): return False
            if os.path.getsize(path) == 0: return False # Treat empty files as invalid
            sha1 = hashlib.sha1()
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
                    unit_divisor=1024, desc=f"Syncing {os.path.basename(path)}", disable=silent, bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]  ") as bar:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk: f.write(chunk); bar.update(len(chunk))
        except Exception as e:
            if not silent: print(f"[ ! ] Error: {e}")
    
    def is_allowed(rules):
        # Strict Linux filtering for library rules.
        if not rules: return True
        allowed = False
        for r in rules:
            # If 'os' is specified, it MUST be linux. If no 'os', it applies to all.
            match = (r.get('os', {}).get('name') == f"{platform_os}") if 'os' in r else True
            if match: allowed = (r['action'] == 'allow')
        return allowed
    
    # SELECT GAME VERSION
    last_v_file = os.path.join(MC_DIR, "cache/last_version.txt")
    manifest_cache = os.path.join(MC_DIR, "cache/manifest.json")
    VERSION, V_URL = None, None
    
    if args.offline and os.path.exists(last_v_file):
        with open(last_v_file, 'r') as f: VERSION = f.read().strip()
        print(f"[ ✅ ] Local Authentication Active: Loading {VERSION}")
    
    if not VERSION:
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
                with open(manifest_cache, 'w') as f: json.dump(manifest, f)
            else:
                with open(manifest_cache, 'r') as f: manifest = json.load(f)
        except:
            if os.path.exists(manifest_cache):
                with open(manifest_cache, 'r') as f: manifest = json.load(f)
            else:
                print("[ ❌ ] Failed to fetch version manifest and no cache available. Check your internet connection.")
                sys.exit(1)
    
        v_pool = [v for v in manifest['versions'] if v['type'] in (['snapshot'] if args.snapshots else (['old_beta', 'old_alpha'] if args.beta else ['release']))]
        last_saved = ""
        if os.path.exists(last_v_file):
            with open(last_v_file, 'r') as f: last_saved = f.read().strip()
    
        # Version menu
        def interactive_select(options, last_saved=""):
    
            def get_linux_key():
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setraw(sys.stdin.fileno())
                    ch = sys.stdin.read(1)
                    # Handle Arrow keys
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
                    window_size = max(5, term_height - 7) 
                except:
                    window_size = 15
    
                print("\033[H\033[J", end="") # os.system('clear') ## Clear Screen
                print(f"------ Choose game version ------\n")
                print(f"Arrows ( ↑ and ↓ ): Navigate | Enter: Select | Q: Print Mode (for fallback)\n")
    
                # CALCULATE WINDOW SLICE
                # This logic keeps the selection 'curr' within the visible window
                start = max(0, min(curr - window_size // 2, total - window_size))
                end = min(start + window_size, total)
    
                for i in range(start, end):
                    v = options[i]
                    # Use different symbols for selected vs last-used
                    is_selected = (i == curr)
                    is_last = (v['id'] == last_saved)
    
                    sel_prefix = " >> " if is_selected else "    "
                    sel_marker = " [ X ]" if is_selected else " [   ]" # Future Plan
    
                    line = f"{sel_prefix}{v['id']} ({v['type']})"
                    if is_last:
                        line += "  <-- (Last Selected)"
    
                    # Print the built line
                    print(line)
    
                print(f"\n[ {curr + 1} / {total} ] | Page: {start+1}-{end}")
    
                # INPUT HANDLING
                key = get_linux_key()
                if key == '\x1b[A': # If UP Arrow key is pressed,
                    curr = max(0, curr -1)
                elif key == '\x1b[B': # If DOWN Arrow key is pressed,
                    curr = min(total - 1, curr + 1)
                elif key in ('\r', '\n'): # If ENTER key is pressed (Carriage_Return-Line_Feed),
                    return options[curr]
                elif key.lower() == 'q': # Quit if 'Q' key is pressed
                    print("\033[H\033[J", end="") # os.system('clear') ## Clear Screen
                    return None
        
        # Interactive Menu comes first
        selected_obj = interactive_select(v_pool, last_saved)
        
        if selected_obj:
            VERSION, V_URL = selected_obj['id'], selected_obj['url']
            with open(last_v_file, 'w') as f: f.write(VERSION)
        else:
            while True:
                # FALLBACK to manual input if user quits less
                print("\n  ---- Game VERSION LIST ----")
                menu = "\n".join([f"    {i+1}. {v['id']} ({v['type']}) {' <-- [LAST SELECTED]' if v['id'] == last_saved else ''}" for i, v in enumerate(v_pool)])
                try: subprocess.run(["less", "-X"], input=menu, text=True, check=True) # Try to run less
                except: print(menu) # Print Everything Fallback
                sel = input(f"\nSelect Version [Default: {last_saved}]: ").strip()
                if not sel and last_saved:
                    VERSION = last_saved
                    V_URL = next(v['url'] for v in v_pool if v['id'] == VERSION)
                    break
                try:
                    idx = int(sel) - 1
                    if 0 <= idx < len(v_pool):
                        VERSION, V_URL = v_pool[idx]['id'], v_pool[idx]['url']
                        with open(last_v_file, 'w') as f: f.write(VERSION)
                        break
                except: pass
    
    # CHECK RUNTIME ASSETS & NATIVES
    v_root = os.path.join(MC_DIR, f"versions/{VERSION}")
    v_json_path = os.path.join(v_root, f"{VERSION}.json")
    integrity_marker = os.path.join(v_root, ".integrity_passed")
    
    if not args.offline:
        if args.refresh or not os.path.exists(v_json_path):
            get(V_URL, v_json_path, silent=True)
    
    with open(v_json_path, 'r') as f: v_json = json.load(f)
    
    jar_path = os.path.join(v_root, f"{VERSION}.jar")
    
    # Only download jar if marker is missing
    if not os.path.exists(integrity_marker) and not args.offline:
        get(v_json['downloads']['client']['url'], jar_path, v_json['downloads']['client'].get('sha1'))
    
    cp_paths, lib_queue, natives_queue = [jar_path], [], []
    
    # Mapping variables
    natives_dir = os.path.join(v_root, '${natives_directory}')
    os.makedirs(natives_dir, exist_ok=True)
    
    # Parse Libraries (for Linux)
    for lib in v_json['libraries']:
        if not is_allowed(lib.get('rules')): continue
        dl = lib.get('downloads', {})
        if 'artifact' in dl:
            lp = os.path.join(MC_DIR, "libraries", dl['artifact']['path'])
            lib_queue.append((dl['artifact']['url'], lp, dl['artifact'].get('sha1')))
            cp_paths.append(lp)
        # Explicitly look for Linux natives
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
        if not os.path.exists(integrity_marker):
            get(v_json['assetIndex']['url'], a_path, v_json['assetIndex'].get('sha1'), silent=True)
        if os.path.exists(a_path):
            with open(a_path, 'r') as f:
                objs = json.load(f).get('objects', {})
                res_link = b64d("aHR0cHM6Ly9yZXNvdXJjZXMuZG93bmxvYWQubWluZWNyYWZ0Lm5ldA==")
                asset_q = [(f"{res_link}/{h[:2]}/{h}", os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}"), h) for h in [d['hash'] for d in objs.values()]]
    
    # INTEGRITY CHECK, RETRY & SUCCESS MARKER
    if args.offline or os.path.exists(integrity_marker):
        print(f"[ ✅ ] Integrity marker found. Skipping verification for {VERSION}.")
    else:
        max_retries = 7
        success = False
    
        for attempt in range(max_retries):
            print(f"\n[ {attempt+1} 🎯 ] Download/Verification Attempt: ( {attempt+1} / {max_retries} )")
    
            # Run Downloads
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                if lib_queue:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), lib_queue), total=len(lib_queue), desc="[ 🔍 ] Downloading & Verifying Libs", bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} files  "))
                if asset_q:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), asset_q), total=len(asset_q), desc="[ 🔍 ] Downloading & Verifying Assets", bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} items  "))
    
            # Final Integrity Check
            missing = []
            for _, path, _ in lib_queue:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
            for _, path, _ in asset_q:
                if not os.path.exists(path) or os.path.getsize(path) == 0: missing.append(path)
    
            if not missing:
                print("[ ✅ ] All files verified successfully.")
                with open(integrity_marker, 'w') as f: f.write("OK")
                success = True
                break
            else:
                print(f"[ ⚠️ ] Warning: {len(missing)} file/s failed to download or are corrupt:")
                for m in missing[:15]: # Log first 15 missing files to stdout
                    print(f" - {os.path.basename(m)}")
                if len(missing) > 15: print(f" ... and {len(missing)-15} more.")
    
                if attempt < max_retries - 1:
                    print("[ ⚠️ ] Retrying missing files in 5 seconds...")
                    time.sleep(5)
        
        if args.old_compatibility:
            # Sound compatibility fix for old versions
            shutil.copytree(os.path.join(MC_DIR, "assets"), os.path.join(MC_DIR, "resources"), dirs_exist_ok=True)
            asset_index_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
            if os.path.exists(asset_index_path):
                with open(asset_index_path, 'r') as f:
                    index_data = json.load(f)
                    objects = index_data.get('objects', {})
                    
                    # tqdm for visual feedback on sound mapping
                    for name, info in tqdm(objects.items(), desc="[ 🔊 ] Reconstructing Legacy Sounds", bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} items  "):
                        h = info['hash']
                        src_file = os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}")
                        dst_file = os.path.join(MC_DIR, "resources", name)
                        
                        if os.path.exists(src_file):
                            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                            if not os.path.exists(dst_file):
                                shutil.copy2(src_file, dst_file)
        
        
        if not success:
            print("\n[ ❌ ] Critical Error: Failed to download required files after multiple attempts.")
            print(f"[ ❌ ] {len(missing)} files are still missing. Aborting launch.")
            sys.exit(1)
    
    # Extract natives (Linux)
    if not os.listdir(natives_dir):
        print(f"[ 📂 ] Extracting Natives... ({platform_os})")
        for np in natives_queue:
            if os.path.exists(np):
                try:
                    with zipfile.ZipFile(np, 'r') as z:
                        for n in [f for f in z.namelist() if f.endswith('.so')]:
                            with z.open(n) as s, open(os.path.join(natives_dir, os.path.basename(n)), "wb") as d: d.write(s.read())
                except: pass
    
        # ATTENTION NEEDED!!! (For linux only) Specifically extract libflite.so from the text2speech library if found
        for lp in cp_paths:
            if "text2speech" in lp and os.path.exists(lp):
                try:
                    with zipfile.ZipFile(lp, 'r') as z:
                        for n in [f for f in z.namelist() if f.endswith('libflite.so')]:
                            with z.open(n) as s, open(os.path.join(natives_dir, "libflite.so"), "wb") as d: d.write(s.read())
                except: pass
    
    # Exit the program if the user only wanted to download game files.
    if args.game_download_only:
        print(f"\n[ ✅ ] Game {VERSION} Downloaded Successfully")
        print(f"\n[ ✅ ] {platform_os} library included...")
        print(f"\n[ BYE ] Exiting...\n")
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
    
        # Base JVM Command
        cmd = [JAVA_BIN, f"-Xmx{max_mb}M", f"-Xms{min_mb}M"]
    
        # AUTOMATIC HUGE PAGES DETECTION
        # Check if Linux and if THP is enabled/supported
        thp_path = "/sys/kernel/mm/transparent_hugepage/enabled"
        use_huge_pages = False
        huge_pages_confirm = False
        intentionally_disabled_huge_pages = False
        if os.path.exists(thp_path):
            with open(thp_path, 'r') as f:
                status = f.read()
                if "[always]" in status or "[madvise]" in status:
                    use_huge_pages = True
        if not args.disable_huge_pages:
            if use_huge_pages:
                cmd.extend(["-XX:+UseLargePages", "-XX:+AlwaysPreTouch"])
                huge_pages_confirm = True
        else:
            intentionally_disabled_huge_pages = True
        
        # Appending remaining flags
        if not args.old_compatibility: cmd.append("--enable-native-access=ALL-UNNAMED")
        
        ## Override OpenAL behaviour
        if not args.force_disable_openal or args.force_openal:
            # Force use of system OpenAL if available
            if os.path.exists("/usr/lib/libopenal.so.1"):
                cmd.append("-Dorg.lwjgl.util.NoChecks=true")
                cmd.append("-Dorg.lwjgl.librarypath=" + natives_dir)
                cmd.append("-Dnet.java.games.input.librarypath=" + natives_dir)
        
        g_launcher_name_part = b64d("bWluZWNyYWZ0")
        cmd.extend([
            f"-Djava.library.path={natives_dir}", 
            f"-Djna.library.path={natives_dir}", 
            f"-D{g_launcher_name_part}.launcher.brand=NuxCraft-PyCher({launcher_version})"
            ])
        
        if JVM_ARGS.strip(): cmd.extend(JVM_ARGS.split())
    
        params = {
            "${auth_player_name}": USERNAME, 
            "${version_name}": VERSION, 
            "${game_directory}": MC_DIR, 
            "${assets_root}": os.path.join(MC_DIR, "assets"), 
            "${assets_index_name}": a_id, 
            "${auth_uuid}": UUID, 
            "${auth_access_token}": "null", 
            "${user_type}": "mojang", 
            "${version_type}": "release", 
            "${natives_directory}": natives_dir, 
            "${classpath}": ":".join(cp_paths) # Linux Classpath Separator
        }
    
        if 'arguments' in v_json:
            for arg in v_json['arguments'].get('jvm', []):
                if isinstance(arg, str): cmd.append(params.get(arg, arg))
                elif isinstance(arg, dict) and is_allowed(arg.get('rules')):
                    val = arg['value'] if isinstance(arg['value'], list) else [arg['value']]
                    cmd.extend([params.get(v, v) for v in val])
            cmd.append(v_json['mainClass'])
            for arg in v_json['arguments'].get('game', []):
                if isinstance(arg, str): cmd.append(params.get(arg, arg))
        else:
            cmd.extend(["-cp", ":".join(cp_paths), v_json['mainClass']])
            game_json_arguments = b64d("bWluZWNyYWZ0QXJndW1lbnRz")
            leg_str = v_json[f"{game_json_arguments}"]
            for k, v in params.items(): leg_str = leg_str.replace(k, v)
            cmd.extend(leg_str.split())
    
        if GAME_ARGS.strip(): cmd.extend(GAME_ARGS.split())
        if args.fullscreen: cmd.append('--fullscreen')
        if DEMO_MODE: cmd.append('--demo')
        return cmd, huge_pages_confirm, intentionally_disabled_huge_pages
    
    final_cmd, huge_pages_active, intentionally_disabled_huge_pages = build_cmd()
    
    if huge_pages_active:
        print("\n[ ✅ ] Transparent Huge Pages (THP) enabled")
    else:
        if intentionally_disabled_huge_pages:
            print("\n[ ℹ️ ] Transparent Huge Pages (THP) has been disabled by the user.")
        else:
            print("\n[ ℹ️ ] NOTE: Transparent Huge Pages (THP) not detected or disabled.\n", 
                  "      For optimal performance, consider enabling THP on your system (Optional).")
    
    print(f"\n[ 👍 ] Finalizing... \n", 
          f"        Game Version: {VERSION}\n", 
          f"        Player Name: {USERNAME}\n", 
          f"        Max Allocated RAM: {MEMORY}\n", 
          f"        Max Thread Count: {MAX_THREAD_COUNT}\n")
    
    if DEMO_MODE: print(f"\n    [ ⚠️ ] WARNING: DEMO MODE enabled...\n", 
                        f"    YES, YOU did it... INTENTIONALLY!!!\n", 
                        f"    Have a nice 1 Hour 40 Minutes DEMO!!!\n")
    
    with open(os.path.join(MC_DIR, "logs/latest_launch.log"), "w") as f:
        f.write(f"    (PLATFORM: {platform_os}) COMMAND EXECUTED:\n\n{' '.join(final_cmd)}\n\n")
        f.write("-" * 25 + " GAME OUTPUT START " + "-" * 25 + "\n\n")
        f.flush()
        
        # Detach and exit
        subprocess.Popen(
            final_cmd, 
            cwd=MC_DIR, 
            stdout=f, 
            stderr=f, 
            start_new_session=True
        )
        
        print("[ ✅ ] Game launch started.")
        print("[ ⏰ ] Please, be patient...\n")
        sys.exit(0)
except KeyboardInterrupt:
    print("\n\n[ 💀 ] Shutdown requested by user. Exiting...\n")
    sys.exit(1)
