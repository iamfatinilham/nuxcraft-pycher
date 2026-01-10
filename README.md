
# NuxCraft-PyCher

The well-known block game (Java Edition) launcher written in Python for Offline (Single Player) and LAN builders (Made for CLI lovers)


<hr>

## ⚠️ Disclaimer: This project is for **educational**, **research** and **testing** purposes only.

  - ## Usage:
    - ## Step 1: Install required packages/applications

    - ## Install Python & Java
      - ### Instructions for Linux 🐧:
        - #### On `Debian (e.g. Ubuntu)` based distros,
          ```bash
          sudo apt update && sudo apt install -y less curl python3 python3-pip python3-venv python-is-python3 default-jdk
          ```
          **Note: You can replace `default-jdk` with `default-jre` if you want less tools on Debian and Debian based distros.**


        - #### On `Archlinux (e.g. Manjaro)` based distros,
          ```bash
          sudo pacman -Sy --noconfirm less curl python python-pip jdk-openjdk
          ```
          **Note: You can replace `jdk-openjdk` with `jre-openjdk` if you want less tools on Arch Linux: By the Way and distros based on it.**


        - #### On `Fedora` based distros,
          ```bash
          sudo dnf update && sudo dnf install -y less curl python3 python3-pip java-latest-openjdk-devel
          ```
          **Note: You can replace `java-latest-openjdk-devel` with `java-latest-openjdk` if you want less tools on Fedora and Fedora based distros.**



      - ### Instructions for Windows 🪟:
        - ### Download and install Python 3:
          - [Python Official website](https://python.org/downloads/windows/)
          #### **Note: You need to check(turn on) the box/option `☑️ Add Python [Python Version] to PATH` while installing Python**
          #### **Optional: You might also want to install Python for All users by checking `☑️ Install launcher for all users (recommended)`**
        - ### Download and install the correct Java version suitable for the game version you want using these links:
          - [Microsoft Build of Java (Latest Releases) [Recommended]](https://learn.microsoft.com/java/openjdk/download)
          - [Microsoft Build of Java (Old Releases) [Recommended]](https://learn.microsoft.com/java/openjdk/older-releases)
        - #### One Extra step needed for Windows:
          - #### Prepeare Windows PowerShell (Optional, but RECOMMENDED)
            - Click `Windows Key` + `R` and paste this line and paste `ENTER`:

              ```ps1
              powershell -Command "Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force"
              ```
              **What this does:**
              - Opens Windows PowerShell
              - Gives the current user (Your account) permission to execute `.ps1` scripts **(Will come in handy for setting up Python virtual environments)**

    ### `Disclaimer: Never copy-paste commands blindly on your computer unless you know what that command does.`


    ### You get the idea, install java, python3, python-pip for your distro/OS



    ### **`Note: Specific game version requires specific Java Version. (Old version of game won't run on newer java version)`**

    <hr>


  - ## Step 2: Download the script (Single file only)


    - ### On Linux 🐧:

      In your terminal, paste this and press `ENTER`

      ```bash
      mkdir -p ./nuxcraft-pycher && cd ./nuxcraft-pycher && curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py -o nuxcraft-pycher.py
      ```

    - ### On Windows 🪟:
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
        curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher-win.py -o nuxcraft-pycher.py
        ```


  - ## Step 3: Make a Python Virtual Environment


    - ### On Linux 🐧:
      ```bash
      python3 -m venv .venv && source .venv/bin/activate
      ```

    - ### On Windows 🪟:
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

  - ## Step 4: Install necessary package

    - ### Paste this (Applicable on both Linux🐧 and Windows🪟):

      ```
      pip install requests
      ```

  ### **Now, it's time... for the real part... (launching the game 🎮)**


  ## Usage/Examples


  You can just simply launch the program while in `nuxcraft-pycher` directory using

  - ### On Linux 🐧:
    ```bash
    python3 ./nuxcraft-pycher.py
    ```

  - ### On Windows 🪟:
    ```ps1
    python .\nuxcraft-pycher.py
    ```

  Then, you can use `⬆️` or `⬇️` to see the version list, after seeing the version list, type the letter `Q` on your keyboard. After that, type the serial number of the version you prefer from the list and press `ENTER` key.


  Then, the script will download the game and launch it.


  **Note: By default, the script will only show stable releases in descending order. You can override this behaviour by using `-s`/ `--snapshots` for snapshot/developer releases and `-b`/ `--beta` for old beta releases.**


  To get started and see help,


  - ### On Linux 🐧:
    ```bash
    python3 ./nuxcraft-pycher.py --help
    ```

  - ### On Windows 🪟:
    ```ps1
    python .\nuxcraft-pycher.py --help
    ```

**Then, you are good to go 😃😃😃**

**Just answer the prompts, and Minecraft will download and launch**


**Note: the script might look broken if your terminal doesn't support unicode and emojis**


### Happy Building ⚒️⚒️⚒️



## FAQ

- #### Is this project safe?

  You don't need to believe me. You can inspect the full code yourself. It's an `open-source` `project` licenced under [GPL-V3](https://www.gnu.org/licenses/gpl-3.0.en.html) with no intent to make money. Free in freedom...

- #### Do, I need any account?

  Single answer, **NO**



## Author
- #### [Fatin Ilham](https://www.github.com/iamfatinilham)


## License

- ### [GPL-V3](https://www.gnu.org/licenses/gpl-3.0.en.html)
