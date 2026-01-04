#!/usr/bin/env python3

import os, json, requests, subprocess, zipfile, sys, argparse, hashlib, time, uuid
from concurrent.futures import ThreadPoolExecutor

############################
##### LAUNCHER VERSION #####
############################
launcher_version = 0.2
############################

try:
    # 1. Generate player UUID
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
        print("[ ⚠️ ] tqdm not found. Installing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm", "requests"])
        from tqdm import tqdm
    
    parser = argparse.ArgumentParser(description=f"NuxCraft-PyCher (Linux) Version: {launcher_version} Created by Fatin Ilham")
    parser.add_argument("-f", "--fullscreen", action="store_true", help="Launch game in fulscreen mode")
    parser.add_argument("--java", type=str, metavar="PATH(BINARY)", default="java", help="Java binary path")
    parser.add_argument("--game-dir", type=str, metavar="PATH(DIRECTORY)", default=".minecraft", help="Custom game directory | Default: .minecraft")
    parser.add_argument("-s", "--snapshots", action="store_true", help="Show snapshot releases")
    parser.add_argument("-b", "--beta", action="store_true", help="Show old beta releases")
    parser.add_argument("-R", "--refresh", action="store_true", help="Fetch version list from internet")
    parser.add_argument("-p", "--player", type=str, metavar="NAME", default="player", help="Set player username | Default: player")
    parser.add_argument("-m", "--memory", type=str, metavar="AMOUNT", default="2G", help="RAM (e.g. 8G) | Default: 4G")
    parser.add_argument("-t", "--threads", type=int, metavar="NUMBER", default=2, help="Allocate max number of threads (e.g. 4) | Default: 2")
    parser.add_argument("--last", "--offline", action="store_true", help="Launch last version instantly")
    parser.add_argument("--jvm-flags", type=str, metavar="FLAGS", default=" ", help="Parse extra flags/arguments for JVM when launching game")
    parser.add_argument("--game-flags", type=str, metavar="FLAGS", default=" ", help="Parse extra flags/arguments for the game when launching game")
    
    args = parser.parse_args()
    
    
    # Check for Linux environment
    if sys.platform != "linux":
        print("[ ❌ ] Error: This script is designed specifically for Linux.")
        sys.exit(1)
    
    # Useful vars (all of them generated on the fly) [better not to edit them]
    USERNAME = args.player
    UUID = generate_offline_uuid(USERNAME)
    JAVA_BIN = args.java
    MC_DIR = os.path.abspath(args.game_dir)
    MEMORY = args.memory
    MAX_THREAD_COUNT = args.threads
    JVM_ARGS = args.jvm_flags
    GAME_ARGS = args.game_flags
    FULLSCREEN = args.fullscreen
    
    
    for folder in ['versions', 'libraries', 'assets/indexes', 'assets/objects', 'cache', 'logs']:
        os.makedirs(os.path.join(MC_DIR, folder), exist_ok=True)
    
    # UTILITIES
    session = requests.Session()
    session.headers.update({"User-Agent": f"NuxCraft-PyCher/{launcher_version} (Linux)"})
    
    def get(url, path, expected_hash=None, silent=False):
        if args.last: return
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
            with session.get(url, timeout=10, stream=True) as r:
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
            match = (r.get('os', {}).get('name') == 'linux') if 'os' in r else True
            if match: allowed = (r['action'] == 'allow')
        return allowed
    
    # 2. SELECT VERSION
    last_v_file = os.path.join(MC_DIR, "cache/last_version.txt")
    manifest_cache = os.path.join(MC_DIR, "cache/manifest.json")
    VERSION, V_URL = None, None
    
    if args.last and os.path.exists(last_v_file):
        with open(last_v_file, 'r') as f: VERSION = f.read().strip()
        print(f"[ ✅ ] Local Authentication Active: Loading {VERSION}")
    
    if not VERSION:
        try:
            if args.refresh or not os.path.exists(manifest_cache):
                r = session.get("https://launchermeta.mojang.com/mc/game/version_manifest.json", timeout=15)
                manifest = r.json()
                with open(manifest_cache, 'w') as f: json.dump(manifest, f)
            else:
                with open(manifest_cache, 'r') as f: manifest = json.load(f)
        except:
            with open(manifest_cache, 'r') as f: manifest = json.load(f)
    
        v_pool = [v for v in manifest['versions'] if v['type'] in (['snapshot'] if args.snapshots else (['old_beta', 'old_alpha'] if args.beta else ['release']))]
        last_saved = ""
        if os.path.exists(last_v_file):
            with open(last_v_file, 'r') as f: last_saved = f.read().strip()
    
        while True:
            menu = "\n".join([f"{i+1}. {v['id']} ({v['type']}) {'[LAST]' if v['id'] == last_saved else ''}" for i, v in enumerate(v_pool)])
            try: subprocess.run(["less", "-X"], input=menu, text=True, check=True)
            except: print(menu)
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
    
    # 3. CHECK RUNTIME ASSETS & NATIVES
    v_root = os.path.join(MC_DIR, f"versions/{VERSION}")
    v_json_path = os.path.join(v_root, f"{VERSION}.json")
    integrity_marker = os.path.join(v_root, ".integrity_passed")
    
    if not args.last: get(V_URL, v_json_path, silent=True)
    with open(v_json_path, 'r') as f: v_json = json.load(f)
    
    jar_path = os.path.join(v_root, f"{VERSION}.jar")
    # Only download jar if marker is missing
    if not os.path.exists(integrity_marker) and not args.last:
        get(v_json['downloads']['client']['url'], jar_path, v_json['downloads']['client'].get('sha1'))
    
    cp_paths, lib_queue, natives_queue = [jar_path], [], []
    
    
    natives_dir = os.path.join(v_root, '${natives_directory}')
    os.makedirs(natives_dir, exist_ok=True)
    
    # Parse Libraries (Strict Linux Logic)
    for lib in v_json['libraries']:
        if not is_allowed(lib.get('rules')): continue
        dl = lib.get('downloads', {})
        if 'artifact' in dl:
            lp = os.path.join(MC_DIR, "libraries", dl['artifact']['path'])
            lib_queue.append((dl['artifact']['url'], lp, dl['artifact'].get('sha1')))
            cp_paths.append(lp)
        # Explicitly look for linux natives
        if 'natives-linux' in dl.get('classifiers', {}):
            n_data = dl['classifiers']['natives-linux']
            np = os.path.join(MC_DIR, "libraries", n_data['path'])
            lib_queue.append((n_data['url'], np, n_data.get('sha1')))
            natives_queue.append(np)
    
    a_id = v_json['assetIndex']['id']
    a_path = os.path.join(MC_DIR, f"assets/indexes/{a_id}.json")
    asset_q = []
    
    # Prepare asset queue
    if not args.last:
        if not os.path.exists(integrity_marker):
            get(v_json['assetIndex']['url'], a_path, v_json['assetIndex'].get('sha1'), silent=True)
        if os.path.exists(a_path):
            with open(a_path, 'r') as f:
                objs = json.load(f).get('objects', {})
                asset_q = [(f"https://resources.download.minecraft.net/{h[:2]}/{h}", os.path.join(MC_DIR, f"assets/objects/{h[:2]}/{h}"), h) for h in [d['hash'] for d in objs.values()]]
    
    # LOGIC: INTEGRITY CHECK, RETRY & SUCCESS MARKER
    if args.last or os.path.exists(integrity_marker):
        print(f"[ ✅ ] Integrity marker found. Skipping verification for {VERSION}.")
    else:
        max_retries = 7
        success = False
    
        for attempt in range(max_retries):
            print(f"\n[ {attempt+1} 🎯 ] Download/Verification Attempt: ( {attempt+1} / {max_retries} )")
    
            # 1. Run Downloads
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                if lib_queue:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), lib_queue), total=len(lib_queue), desc="[ 🔍 ] Checking & Downloading Libs", bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} files  "))
                if asset_q:
                    list(tqdm(ex.map(lambda x: get(x[0], x[1], x[2], silent=True), asset_q), total=len(asset_q), desc="[ 🔍 ] Checking & Downloading Assets", bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} items  "))
    
            # 2. Final Integrity Check
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
                print(f"[ ⚠️ ] Warning: {len(missing)} files failed to download or are corrupt:")
                for m in missing[:15]: # Log first 15 missing files to stdout
                    print(f" - {os.path.basename(m)}")
                if len(missing) > 15: print(f" ... and {len(missing)-15} more.")
    
                if attempt < max_retries - 1:
                    print("[ ⚠️ ] Retrying missing files in 2 seconds...")
                    time.sleep(2)
    
        if not success:
            print("\n[ ❌ ] Critical Error: Failed to download required files after multiple attempts.")
            print(f"[ ❌ ] {len(missing)} files are still missing. Aborting launch.")
            sys.exit(1)
    
    # Extract natives (Modified part: includes libflite.so logic)
    if not os.listdir(natives_dir):
        print("[ 📂 ] Extracting Natives...")
        for np in natives_queue:
            if os.path.exists(np):
                try:
                    with zipfile.ZipFile(np, 'r') as z:
                        for n in [f for f in z.namelist() if f.endswith('.so')]:
                            with z.open(n) as s, open(os.path.join(natives_dir, os.path.basename(n)), "wb") as d: d.write(s.read())
                except: pass
    
        # ATTENTION NEEDED!!! Specifically extract libflite.so from the text2speech library if found
        for lp in cp_paths:
            if "text2speech" in lp and os.path.exists(lp):
                try:
                    with zipfile.ZipFile(lp, 'r') as z:
                        for n in [f for f in z.namelist() if f.endswith('libflite.so')]:
                            with z.open(n) as s, open(os.path.join(natives_dir, "libflite.so"), "wb") as d: d.write(s.read())
                except: pass
    
    # 4. THE Local Authentication EXECUTION
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
        if os.path.exists(thp_path):
            with open(thp_path, 'r') as f:
                status = f.read()
                if "[always]" in status or "[madvise]" in status:
                    use_huge_pages = True
    
        if use_huge_pages:
            cmd.extend(["-XX:+UseLargePages", "-XX:+AlwaysPreTouch"])
        
        # Add remaining standard flags
        cmd.extend(["--enable-native-access=ALL-UNNAMED", f"-Djava.library.path={natives_dir}", f"-Djna.library.path={natives_dir}", f"-Dminecraft.launcher.brand=NuxCraft-PyCher({launcher_version})"])
    
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
            leg_str = v_json['minecraftArguments']
            for k, v in params.items(): leg_str = leg_str.replace(k, v)
            cmd.extend(leg_str.split())
    
        if GAME_ARGS.strip(): cmd.extend(GAME_ARGS.split())
        if FULLSCREEN: cmd.append('--fullscreen')
        return cmd
    
    final_cmd = build_cmd()
    print(f"\n[ 👍 ] Finalizing... \n      Minecraft Version: {VERSION}\n      Player Name: {USERNAME}\n      Max Allocated RAM: {MEMORY}\n      Max Thread Count: {MAX_THREAD_COUNT}\n")
    
    with open(os.path.join(MC_DIR, "logs/latest_launch.log"), "w") as f:
        f.write(f"COMMAND EXECUTED:\n\n{' '.join(final_cmd)}\n\n")
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
        print("[ ⏱️ ] Please, be patient...\n")
        sys.exit(0)
except KeyboardInterrupt:
    print("\n\n[ 💀 ] Shutdown requested by user. Exiting...")
    sys.exit(1)
    