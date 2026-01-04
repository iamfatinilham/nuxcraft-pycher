
# NuxCraft-PyCher

The well-known block game (Java Edition) launcher written in Python for Offline (Single Player) and LAN builders (Made for Linux)

## Prerequisites:

### Step 1: Install Python & Java

#### On `Debian (e.g. Ubuntu)` based distros,

```bash
sudo apt update && sudo apt install -y less curl python3 python3-pip python3-venv python-is-python3 default-jdk default-jre
```



#### On `Archlinux` based distros,

```bash
sudo pacman -Sy --noconfirm less curl python python-pip jdk-openjdk jre-openjdk
```



#### On `Fedora` based distros,

```bash
sudo dnf update && sudo dnf install -y less curl python3 python3-pip java-latest-openjdk java-latest-openjdk-devel
```


#### You get the idea, install java, python3, python-pip for your distro


**`Note: correct version of Java is required for running the version you want`**


## Step 2: Clone the repo


```bash
mkdir -p ./nuxcraft-pycher && cd ./nuxcraft-pycher && curl -L https://raw.githubusercontent.com/iamfatinilham/nuxcraft-pycher/main/nuxcraft-pycher.py -o nuxcraft-pycher.py
```


## Step 3: Make a Python Virtual Environment

```bash
python3 -m venv .minecraft-venv && source .minecraft-venv/bin/activate
```

## Step 4: Install necessary package

```bash
pip install requests
```

**Now, it's time... for the real part... (launching the game)**


## Usage/Examples


You can just simply launch the program while in `nuxcraft-pycher` directory using


```bash
python3 ./nuxcraft-pycher.py
```

Then, you can use `↑` or `↓` to see the version list, after seeing the version list, type the letter `Q` on your keyboard. After that, type the serial number of the version you prefer from the list and press `ENTER` key.


Then, the script will download the game and launch it.


**Note: By default, the script will only show stable releases in descending order. You can override this behaviour by using `-s`/ `--snapshots` for snapshot/developer releases and `-b`/ `--beta` for old beta releases.**


To get started and see help,

```bash
python3 ./nuxcraft-pycher.py --help
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
