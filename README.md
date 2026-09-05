# NuxCraft-PyCher

The well-known block game (Java Edition) launcher written in Python for Offline (Single Player) and LAN builders (Made for CLI lovers)

---

## ⚠️ Disclaimer: This project is for **educational**, **research** and **testing** purposes only.

## Usage

### Step 1: Install required packages/applications

#### Install Python & Java

##### Instructions for Linux 🐧:

###### On `Debian (e.g. Ubuntu)` based distros,
```bash
sudo apt update && sudo apt install -y less curl python3 python3-pip python3-venv python-is-python3 libopenal1 libopenal-dev libflite1 flite1-dev default-jdk
```
**Note: You can replace `default-jdk` with `default-jre` if you want less tools on Debian and Debian based distros.**

###### On `Archlinux (e.g. Manjaro)` based distros,
```bash
sudo pacman -Sy --noconfirm less curl python python-pip openal flite jdk-openjdk
```
**Note: You can replace `jdk-openjdk` with `jre-openjdk` if you want less tools on Arch Linux: By the Way and distros based on it.**

###### On `Fedora` based distros,
```bash
sudo dnf update && sudo dnf install -y less curl python3 python3-pip openal-soft openal-soft-devel flite flite-devel java-latest-openjdk-devel
```
**Note: You can replace `java-latest-openjdk-devel` with `java-latest-openjdk` if you want less tools on Fedora and Fedora based distros.**

###### If you want custom java version to run old game version:
- [Java (Eclipse Adoptium) [Recommended for Linux]](https://adoptium.net/temurin/releases?mode=filter&os=linux&arch=any)
- [Microsoft Build of Java (Latest Releases)](https://learn.microsoft.com/java/openjdk/download)
- [Microsoft Build of Java (Old Releases)](https://learn.microsoft.com/java/openjdk/older-releases)

**Note: you can also download the *.zip* file, extract the *.zip* file. And while launching the game, just point the full path of the **java** binary using `--java` flag on the script.**

##### Instructions for Windows 🪟:

###### Download and install Python 3:
- [Python Official website](https://python.org/downloads/windows/)

**Note: You need to check(turn on) the box/option `☑️ Add Python [Python Version] to PATH` while installing Python**
**Optional: You might also want to install Python for All users by checking `☑️ Install launcher for all users (recommended)`**

###### Download and install the correct Java version suitable for the game version you want using these links:
- [Microsoft Build of Java (Latest Releases) [Recommended for Windows]](https://learn.microsoft.com/java/openjdk/download)
- [Microsoft Build of Java (Old Releases) [Recommended for Windows]](https://learn.microsoft.com/java/openjdk/older-releases)
- [Java (Eclipse Adoptium)](https://adoptium.net/temurin/releases?mode=filter&os=windows&arch=any)

**Note: you can also download the *.zip* file, extract the *.zip* file. And while launching the game, just point the full path of the **java** binary using `--java` flag on the script.**

###### One Extra step needed for Windows:
**Prepeare Windows PowerShell (Optional, but RECOMMENDED)**
Click `Windows Key` + `R` and paste this line and press `ENTER`:

```ps1
powershell -Command "Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force"
```
**What this does:**
- Opens Windows PowerShell
- Gives the current user (Your account) permission to execute `.ps1` scripts **(Will come in handy for setting up Python virtual environments)**

##### Instructions for macOS 🍎:

###### Install Homebrew (if you don't have it already):
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

###### Install Python, Java and dependencies using Homebrew:
```bash
brew install python curl openal-soft openjdk
```

**Note: After installing, you may need to symlink Java so the system can find it:**
```bash
sudo ln -sfn $(brew --prefix openjdk)/libexec/openjdk.jdk /Library/Java/JavaVirtualMachines/openjdk.jdk
```

###### If you want custom java version to run old game version:
- [Java (Eclipse Adoptium) [Recommended for macOS]](https://adoptium.net/temurin/releases?mode=filter&os=mac&arch=any)
- [Microsoft Build of Java (Latest Releases)](https://learn.microsoft.com/java/openjdk/download)
- [Microsoft Build of Java (Old Releases)](https://learn.microsoft.com/java/openjdk/older-releases)

**Note: you can also download the *.tar.gz* file, extract the *.tar.gz* file. And while launching the game, just point the full path of the **java** binary using `--java` flag on the script.**

##### Instructions for FreeBSD 😈:

###### Install Python, Java and dependencies using pkg:
```bash
sudo pkg install -y python3 py311-pip curl openal-soft openjdk21
```
**Note: You can replace `openjdk21` with a different version (e.g. `openjdk17`, `openjdk8`) depending on the game version you want to play.**

###### If you want custom java version to run old game version:
- [Java (Eclipse Adoptium)](https://adoptium.net/temurin/releases?mode=filter&arch=any)

**Note: you can also download the *.tar.gz* file, extract the *.tar.gz* file. And while launching the game, just point the full path of the **java** binary using `--java` flag on the script.**

---

### `Disclaimer: Never copy-paste commands blindly on your computer unless you know what that command does.`

### You get the idea, install java, python3, python-pip for your distro/OS

> [!NOTE]
> **Text-To-Speech (TTS) Native Libraries across OSes:**
> - **Linux:** Uses `libflite.so` (Flite engine), which `nuxcraft-pycher.py` automatically extracts from the bundled library JAR into the native binaries directory.
> - **Windows:** Uses the built-in Windows SAPI (Speech API) / `FliteWrapper.dll`, which the script automatically extracts as `.dll` files.
> - **macOS:** Uses the native Cocoa `SpeechSynthesis` framework / `.dylib` libraries, extracted automatically by the launcher script.
> - **FreeBSD:** Handled via standard OpenAL / system audio bindings.
> 
> No manual file placement is required on any operating system; native libraries are managed fully automatically by the script.

**`Note: Specific game version requires specific Java Version. (Old version of game won't run on newest Java version)` You can use `--java` flag to point your script to that Java binary *(You need to provide the full path of the `java` binary)***


**Tip: Before downloading the game, you can know the required Java version using the `--cj` / `--check-java` flag on the main script.**

---

### Step 2: Download the script (Single file only)

> [!NOTE]  
> The links below download the **stable release** (`main` branch). If you want to test the cutting-edge features and latest bug fixes, replace `main` in the URLs with `refs/heads/development` to download the **development branch** script!
> Example URL: `https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/refs/heads/development/nuxcraft-pycher.py`

#### On Linux 🐧:

In your terminal, paste this and press `ENTER`

```bash
mkdir -p ./nuxcraft-pycher && cd ./nuxcraft-pycher && curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py -o nuxcraft-pycher.py
```

#### On Windows 🪟:
- Click `Windows Key` + `R`
- Type `powershell` (Recommended) or `cmd` and press `ENTER`
- Paste these in sequence:
  ```ps1
  mkdir .\nuxcraft-pycher
  ```

  ```ps1
  cd .\nuxcraft-pycher
  ```

  ```ps1
  Invoke-WebRequest -Uri "https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py" -OutFile "nuxcraft-pycher.py"
  ```

#### On macOS 🍎:

In your terminal, paste this and press `ENTER`

```bash
mkdir -p ./nuxcraft-pycher && cd ./nuxcraft-pycher && curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py -o nuxcraft-pycher.py
```

#### On FreeBSD 😈:

In your terminal, paste this and press `ENTER`

```bash
mkdir -p ./nuxcraft-pycher && cd ./nuxcraft-pycher && curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py -o nuxcraft-pycher.py
```

---

### Step 3: Make a Python Virtual Environment

#### On Linux 🐧:
```bash
python3 -m venv .venv && source .venv/bin/activate
```

#### On Windows 🪟:
```ps1
python -m venv .venv
```

- If on Powershell:
  ```ps1
  .venv\Scripts\Activate.ps1
  ```

- If on Command Prompt (cmd):
  ```ps1
  .venv\Scripts\activate.bat
  ```

#### On macOS 🍎:
```bash
python3 -m venv .venv && source .venv/bin/activate
```

#### On FreeBSD 😈:
```bash
python3 -m venv .venv && source .venv/bin/activate
```

---

### Step 4: Install necessary package

Paste this (Applicable on all platforms):

```
pip install requests
```

---

### **Now, it's time... for the real part... (launching the game 🎮)**

## Usage/Examples

You can just simply launch the program while in `nuxcraft-pycher` directory using

#### On Linux 🐧:
```bash
python3 ./nuxcraft-pycher.py
```

#### On Windows 🪟:
```ps1
python .\nuxcraft-pycher.py
```

#### On macOS 🍎:
```bash
python3 ./nuxcraft-pycher.py
```

#### On FreeBSD 😈:
```bash
python3 ./nuxcraft-pycher.py
```

Then, you can use `⬆️` or `⬇️` to see the version list, after seeing the version list, type the letter `Q` on your keyboard. After that, type the serial number of the version you prefer from the list and press `ENTER` key.

Then, the script will download the game and launch it.

**Note: By default, the script will only show stable releases in descending order. You can override this behaviour by using `-s`/ `--snapshots` for snapshot/developer releases and `-b`/ `--beta` for old beta releases.**

**Also Note 😅: If you want to play *older versions* then, don't forget to use `-O` or, `--old` along with compatible Java version with `--java`**

**If you forgot to use `-O` or, `--old` while downloading older version of the game, and facing sound not working issue, then delete the `.integrity_passed` file located at `.game > versions > {VERSION NUMBER}` directory/folder and run the script again with `-O` or, `--old` flag. This might fix your sound issue.**

To get started and see help,

#### On Linux 🐧:
```bash
python3 ./nuxcraft-pycher.py --help
```

#### On Windows 🪟:
```ps1
python .\nuxcraft-pycher.py --help
```

#### On macOS 🍎:
```bash
python3 ./nuxcraft-pycher.py --help
```

#### On FreeBSD 😈:
```bash
python3 ./nuxcraft-pycher.py --help
```

**Then, you are good to go 😃😃😃**

**Just answer the prompts, and the game will download and launch**

**Note: the script might look broken if your terminal doesn't support unicode and emojis**

### Happy Building ⚒️⚒️⚒️

---

## FAQ

#### Is this project safe?

You don't need to believe me. You can inspect the full code yourself. It's an `open-source` `project` licenced under [GPL-V3](https://www.gnu.org/licenses/gpl-3.0.en.html) with no intent to make money. Free in freedom...

---

## Author
#### [Fatin Ilham](https://www.github.com/iamfatinilham)

## License
### [GPL-V3](https://www.gnu.org/licenses/gpl-3.0.en.html)
