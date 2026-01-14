#!/usr/bin/env python3

import argparse, requests, sys, base64, os, json, subprocess

## ⚠️ Disclaimer: This project is for educational, research and testing purposes only.

############################
##### LAUNCHER VERSION #####
############################
launcher_version = 0.5
############################

try:
    from tqdm import tqdm
except ImportError:
    print("[ ⚠️️ ] tqdm not found. Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"])
    from tqdm import tqdm

# Platform-specific
if sys.platform == "linux":
    import termios, tty
if sys.platform == "win32":
    import msvcrt
    os.system('')

# Simple thing... You know but do not say...
b64d = lambda dta: base64.b64decode(dta).decode('utf-8')

def get_linux_key():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

def interactive_select(options):
    if not sys.stdout.isatty():
        return None

    total = len(options)
    curr = 0

    while True:
        try:
            term_height = os.get_terminal_size().lines
            window_size = max(5, term_height - 8)
        except:
            window_size = 15

        # Clear Screen
        if sys.platform == "linux":
            print("\033[H\033[J", end="")
        if sys.platform == "win32":
            os.system('cls')

        print(f"\n------ Choose game version ------\n")
        print(f"Arrows ( ↑ and ↓ ): Navigate | Enter: Select | Q: Print Mode (for fallback)\n")

        start = max(0, min(curr - window_size // 2, total - window_size))
        end = min(start + window_size, total)

        for i in range(start, end):
            v = options[i]
            is_selected = (i == curr)
            sel_prefix = " >> " if is_selected else "    "
            sel_marker = " [ X ]" if is_selected else " [   ]" # Future Plan
            print(f"{sel_prefix}{v['id']} ({v['type']})")

        print(f"\n[ {curr + 1} / {total} ] | Page: {start+1}-{end}")

        # Input Handling
        if sys.platform == "linux":
            key = get_linux_key()
            if key == '\x1b[A': curr = max(0, curr - 1)
            elif key == '\x1b[B': curr = min(total - 1, curr + 1)
            elif key in ('\r', '\n'): return options[curr]
            elif key.lower() == 'q': return None
        elif sys.platform == "win32":
            key = msvcrt.getch()
            if key == b'\xe0': 
                key = msvcrt.getch()
                if key == b'H': curr = max(0, curr - 1)
                elif key == b'P': curr = min(total - 1, curr + 1)
            elif key in (b'\r', b'\n'): return options[curr]
            elif key.lower() == b'q': return None

def get_java_major_version(v_json_url, v_id):
    try:
        response = requests.get(v_json_url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        return data.get('javaVersion', {}).get('majorVersion', "8")
    except (requests.exceptions.RequestException, Exception):
        print(f"  [ ❌ ] Cannot fetch required Java version info {v_id}")
        return "\033[1;91mUnknown (Fetch Error)\033[0m"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-R", "--refresh", action="store_true", dest="refresh", help="  Fetch version list from internet")
    parser.add_argument("-s", "--snapshots", action="store_true", help="  Show snapshot releases")
    parser.add_argument("-b", "--beta", action="store_true", help="  Show old beta releases")
    args = parser.parse_args()

    manifest_cache = "manifest.json"
    session = requests.Session()

    # Load/Fetch Manifest
    manifest = None
    if args.refresh or not os.path.exists(manifest_cache):
        manifest_json_remote_source1 = b64d("aHR0cHM6Ly9sYXVuY2hlcm1ldGEubW9qYW5nLmNvbS9tYy9nYW1lL3ZlcnNpb25fbWFuaWZlc3QuanNvbg==")
        manifest_json_remote_source2 = b64d("aHR0cHM6Ly9waXN0b24tbWV0YS5tb2phbmcuY29tL21jL2dhbWUvdmVyc2lvbl9tYW5pZmVzdC5qc29u")
        try:
            try:
                r = session.get(manifest_json_remote_source1, timeout=15)
                r.raise_for_status()
            except requests.exceptions.RequestException:
                print(f"  [ ❌ ] Cannot fetch version list from {manifest_json_remote_source1}")
                print(f"     Trying {manifest_json_remote_source2}")
                r = session.get(manifest_json_remote_source2, timeout=15)
                r.raise_for_status()
            
            # Extract data FIRST, then save to file
            manifest = r.json()

            with open(manifest_cache, 'w') as f: json.dump(manifest, f)
        except Exception:
            print(f"  [ ❌ ] \033[1;91mCRITICAL:\033[0m Failed to fetch manifest.")
            if os.path.exists(manifest_cache):
                print("Attempting to load saved cache...")
            else:
                print("  [ ❌ ] \033[1;91mCRITICAL:\033[0m No local cache found. Check your internet connection.")
                return # Give up if no network and no cache
    
    # Load from cache if manifest is still None
    if manifest is None:
        if os.path.exists(manifest_cache):
            try:
                with open(manifest_cache, 'r') as f:
                    manifest = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                print("  [ ❌ ] \033[1;91mCRITICAL:\033[0m Local manifest cache missing or corrupted.")
                return
        else:
            print("\033[1;91mNo data available.\033[0m")
            return


    # Filter Versions
    types = ['release']
    if args.snapshots: types =['snapshot']
    if args.beta: types = ['old_beta', 'old_alpha']
    
    v_pool = [v for v in manifest['versions'] if v['type'] in types]

    # Selection
    selected_obj = interactive_select(v_pool)

    if not selected_obj:
        sys.exit(0)

    print(f"\nSelected: {selected_obj['id']}")
    print(f"Fetching Java requirement...")
    
    java_v = get_java_major_version(selected_obj['url'], selected_obj['id'])
    print(f"  [ ☕ ] Required Java Major Version: \033[1;92m{java_v}\033[0m")

if __name__ == "__main__":
    main()