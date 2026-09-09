#!/usr/bin/env bash
#
# CoreYard installer — https://github.com/Luxorium/coreyard
#
# Sets up CoreYard on any Linux distribution:
#   * installs the system packages it needs (Python 3 + venv/pip, smbclient)
#   * creates a private virtualenv and installs the Python dependencies
#   * seeds a .env from .env.example
#   * installs a ./coreyard launcher
#   * verifies the install by running the offline test suite
#
# Usage:
#   ./install.sh                 # normal install (asks before using sudo)
#   ./install.sh --no-deps       # skip system packages (they're already present)
#   ./install.sh --yes           # never prompt; assume yes
#   ./install.sh --home DIR      # where this installation's own files live
#   ./install.sh --help
#
# The package is installed into the virtualenv in editable mode, so `coreyard` works from
# any directory. Code lives here; configuration, state, locks and output live under the
# data home, which is this checkout when it already holds an installation and
# $XDG_DATA_HOME/coreyard otherwise. Two installations on one host stay separate by giving
# each its own --home.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

SKIP_DEPS=0
ASSUME_YES=0
DATA_HOME=""

# ---------------------------------------------------------------- output ----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
    C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

step() { printf '%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"; }
ok()   { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
die()  { printf '\n%serror:%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; exit 1; }

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ -t 0 ] || return 1              # non-interactive and no --yes: decline
    printf '  %s?%s %s [Y/n] ' "$C_YELLOW" "$C_RESET" "$1"
    read -r reply
    case "$reply" in ""|[yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-deps) SKIP_DEPS=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --home)    shift; [ "$#" -gt 0 ] || die "--home needs a directory"; DATA_HOME="$1" ;;
        --home=*)  DATA_HOME="${1#--home=}" ;;
        --help|-h) usage ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# Where this installation's own files go. The checkout wins when it already holds an
# installation, so re-running the installer on a live yard moves nothing — the state
# database, the order queue and the locks stay exactly where every scheduled job expects
# them. This mirrors `coreyard.config._data_root`, which decides the same thing at runtime.
if [ -z "$DATA_HOME" ]; then
    if [ -n "${COREYARD_HOME:-}" ]; then
        DATA_HOME="$COREYARD_HOME"
    elif [ -f "$REPO_DIR/.env" ] || [ -d "$REPO_DIR/out" ] \
         || ls "$REPO_DIR"/*.sqlite3 >/dev/null 2>&1; then
        DATA_HOME="$REPO_DIR"
    else
        DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/coreyard"
    fi
fi
mkdir -p "$DATA_HOME" || die "cannot create the data home: $DATA_HOME"
DATA_HOME="$(cd "$DATA_HOME" && pwd)"

printf '\n%sCoreYard installer%s  ·  yard inventory → Shopify\n\n' "$C_BOLD" "$C_RESET"

# ------------------------------------------------------- system packages ----
detect_pm() {
    for pm in apt-get dnf yum pacman zypper apk; do
        command -v "$pm" >/dev/null 2>&1 && { echo "$pm"; return; }
    done
    echo ""
}

# Package names differ per distro; smbclient in particular.
packages_for() {
    case "$1" in
        apt-get) echo "python3 python3-venv python3-pip smbclient" ;;
        dnf|yum) echo "python3 python3-pip samba-client" ;;
        pacman)  echo "python python-pip smbclient" ;;
        zypper)  echo "python3 python3-pip samba-client" ;;
        apk)     echo "python3 py3-pip samba-client" ;;
    esac
}

install_cmd() {
    case "$1" in
        apt-get) echo "$SUDO apt-get install -y" ;;
        dnf)     echo "$SUDO dnf install -y" ;;
        yum)     echo "$SUDO yum install -y" ;;
        pacman)  echo "$SUDO pacman -S --needed --noconfirm" ;;
        zypper)  echo "$SUDO zypper --non-interactive install" ;;
        apk)     echo "$SUDO apk add --no-cache" ;;
    esac
}

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

step "Checking system packages"
PM="$(detect_pm)"
if [ "$SKIP_DEPS" -eq 1 ]; then
    ok "skipped (--no-deps)"
elif [ -z "$PM" ]; then
    warn "no supported package manager found (apt/dnf/yum/pacman/zypper/apk)"
    warn "install manually: Python 3.$PY_MIN_MINOR+, its venv and pip modules, and smbclient"
else
    PKGS="$(packages_for "$PM")"
    ok "detected $PM"
    if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
        warn "not root and sudo is unavailable — skipping package install"
        warn "needed: $PKGS"
    elif confirm "install: $PKGS ?"; then
        [ "$PM" = "apt-get" ] && $SUDO apt-get update
        # shellcheck disable=SC2086
        $(install_cmd "$PM") $PKGS || warn "package install reported an error; continuing"
        ok "system packages installed"
    else
        warn "declined — continuing with what is already installed"
    fi
fi

# ---------------------------------------------------------------- python ----
step "Checking Python"
PYTHON=""
for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
        PYTHON="$candidate"; break
    fi
done
[ -n "$PYTHON" ] || die "Python $PY_MIN_MAJOR.$PY_MIN_MINOR or newer is required but was not found."
ok "$($PYTHON --version 2>&1) at $(command -v "$PYTHON")"

command -v smbclient >/dev/null 2>&1 \
    && ok "smbclient present" \
    || warn "smbclient not found — photo fetching will fail until it is installed"

# ----------------------------------------------------------------- venv -----
step "Creating the virtualenv"
if [ -d "$VENV_DIR" ]; then
    ok "reusing $VENV_DIR"
else
    if "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
        ok "created $VENV_DIR"
    else
        # Some distros ship Python with ensurepip stripped out (Debian being the
        # common case). Build the venv without pip, then bootstrap pip into it.
        warn "python -m venv failed; retrying without pip and bootstrapping it"
        "$PYTHON" -m venv --without-pip "$VENV_DIR" \
            || die "could not create a virtualenv — install your distro's python3-venv package"
        if command -v curl >/dev/null 2>&1; then
            curl -sS https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python"
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python"
        else
            die "need curl or wget to bootstrap pip"
        fi
        ok "pip bootstrapped"
    fi
fi

VENV_PY="$VENV_DIR/bin/python"
[ -x "$VENV_PY" ] || die "virtualenv looks broken: $VENV_PY is missing"

step "Installing Python dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip; continuing"
"$VENV_PY" -m pip install --quiet -r "$REPO_DIR/requirements.txt" \
    || die "dependency install failed (see the pip output above)"
ok "$(grep -cE '^[a-zA-Z]' "$REPO_DIR/requirements.txt") package(s) installed from requirements.txt"

# Editable, on purpose. A plain `pip install .` copies the code into site-packages, and
# `coreyard.config` then resolves this installation's data root to $XDG_DATA_HOME rather
# than to the checkout — silently relocating a live yard's state, order queue and locks away
# from every scheduled job that has been finding them here. Editable keeps the checkout as
# the installation while still putting `coreyard` on the venv's PATH and making the package
# importable from any working directory, which is the part that was missing.
#
# A non-editable install is a supported way to run CoreYard — the wheel carries its own
# example yard and config templates, and CI installs one into a clean environment and runs
# the quickstart — it is simply not what an installer pointed at a source checkout should do.
step "Installing CoreYard into the virtualenv"
"$VENV_PY" -m pip install --quiet --no-deps -e "$REPO_DIR" \
    || die "could not install the coreyard package (see the pip output above)"
ok "coreyard installed (editable) — $VENV_DIR/bin/coreyard"

# ------------------------------------------------------------------ env -----
step "Configuring"
ok "data home: $DATA_HOME"
# Deliberately NOT seeded from .env.example. `coreyard init` is the documented way to write
# a working .env — it asks the questions, writes only the keys a site has to decide, and
# chmods the result 0600 — and it refuses to clobber an existing file. An installer that
# creates .env first therefore makes the very next documented command fail with "already
# exists", which is what `./install.sh && coreyard init --demo` used to do on every clean
# machine. `.env.example` stays the full reference for anyone who wants to write one by hand.
if [ -f "$DATA_HOME/.env" ]; then
    ok ".env already exists — left untouched"
else
    warn "no .env yet — run 'coreyard init' (or 'coreyard init --demo') to write one"
    NEEDS_CONFIG=1
fi

# Deliberately NOT auto-copied: a schema.json still full of _TABLE/_COLUMN placeholders would
# look configured and then fail with a confusing SQL error. Absent, the CLI prints a clear
# next step, and `coreyard init --write-schema` puts the template in place when it is wanted.
if [ -f "$DATA_HOME/schema.json" ]; then
    ok "schema.json present"
else
    warn "no schema.json yet — CoreYard ships no vendor schema, you map your own"
    NEEDS_SCHEMA=1
fi

# --------------------------------------------------------------- launcher ---
mkdir -p "$REPO_DIR/bin"
cat > "$REPO_DIR/bin/coreyard" <<'LAUNCHER'
#!/usr/bin/env bash
# CoreYard launcher — runs the CLI inside the project virtualenv.
#
# Deliberately holds no command list. Routing lives in coreyard/cli.py, where `--help`
# can show it, a test can walk it, and code review can see it change. A `case` statement
# here was invisible to all three: it named module paths that a refactor could rename out
# from under it, and the only thing checking them was a regex over this file.
#
#   coreyard --help          every command
set -euo pipefail
# Resolve through symlinks before locating the installation. The installer offers to put
# this on your PATH with `ln -s`, and BASH_SOURCE is then the *link*
# (~/.local/bin/coreyard), so taking its dirname looked for the virtualenv in ~/.local and
# reported it missing. The loop rather than `readlink -f` because it also handles a chain of
# links, and relative ones.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    LINKDIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    case "$SOURCE" in /*) ;; *) SOURCE="$LINKDIR/$SOURCE" ;; esac
done
HERE="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || { echo "coreyard: virtualenv missing — run ./install.sh" >&2; exit 1; }
# Where this installation's own files live, chosen when the installer ran. Exported rather
# than inferred so the answer cannot change with the working directory, and honouring an
# existing value so one host can run several installations.
export COREYARD_HOME="${COREYARD_HOME:-@DATA_HOME@}"
# No `cd` into the installation. It used to be required to make the package importable, and
# it silently made every relative path in .env mean "relative to the checkout" — so the same
# configuration resolved differently for a shell and for the one cron job with no `cd` in
# front of it. The package is installed into the virtualenv now, so the CLI is importable
# from anywhere and paths resolve against COREYARD_HOME.
#
# -u: scheduled runs redirect into a log file, where block buffering would hold a job's
# output back for minutes. `coreyard doctor` decides the order poller is alive by watching
# orders.log grow, so a buffered log is indistinguishable from a dead poller.
exec "$PY" -u -m coreyard "$@"
LAUNCHER
# $HERE when the data home is the checkout, so moving the directory moves both together;
# the literal path otherwise.
if [ "$DATA_HOME" = "$REPO_DIR" ]; then
    LAUNCHER_HOME='$HERE'
else
    LAUNCHER_HOME="$DATA_HOME"
fi
python3 - "$REPO_DIR/bin/coreyard" "$LAUNCHER_HOME" <<'SUBST'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text().replace("@DATA_HOME@", sys.argv[2]))
SUBST
chmod +x "$REPO_DIR/bin/coreyard"
ok "installed bin/coreyard launcher (data home: $DATA_HOME)"

# ---------------------------------------------------------------- verify ----
step "Verifying the install"
if (cd "$REPO_DIR" && "$VENV_PY" -m unittest discover -s tests >/dev/null 2>&1); then
    ok "offline test suite passed"
else
    die "the test suite failed — the install is not healthy"
fi

# ------------------------------------------------------------------ done ----
printf '\n%sCoreYard is installed.%s\n\n' "$C_GREEN$C_BOLD" "$C_RESET"
printf '  code        %s\n' "$REPO_DIR"
printf '  data        %s   (config, state, locks, out/)\n\n' "$DATA_HOME"

# Offered rather than assumed: ~/.local/bin is not on every PATH, and a symlink into a
# directory the shell does not search is a command that mysteriously does not exist.
if [ ! -e "$HOME/.local/bin/coreyard" ] && confirm "put 'coreyard' on your PATH (~/.local/bin)?"; then
    mkdir -p "$HOME/.local/bin"
    ln -sf "$REPO_DIR/bin/coreyard" "$HOME/.local/bin/coreyard"
    ok "linked ~/.local/bin/coreyard"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) CLI="coreyard" ;;
        # The reader is standing in the checkout, having just run ./install.sh, so the
        # relative launcher is both correct and short. Printing an absolute path in every
        # line below buries the commands in it.
        *) warn "~/.local/bin is not on your PATH — add it, or use ./bin/coreyard"
           CLI="bin/coreyard" ;;
    esac
elif [ -e "$HOME/.local/bin/coreyard" ]; then
    CLI="coreyard"
else
    CLI="bin/coreyard"
fi

if [ "${NEEDS_SCHEMA:-0}" -eq 1 ]; then
    printf '\nFirst: map your database\n'
    printf '  cp %s \\\n     %s\n' \
        "$REPO_DIR/coreyard/examples/schema.example.json" "$DATA_HOME/schema.json"
    printf '  %s schema                                    lists your tables\n' "$CLI"
    printf '  then replace every name ending in _TABLE or _COLUMN with your own\n'
fi
if [ "${NEEDS_CONFIG:-0}" -eq 1 ]; then
    printf '\nNext: configure this installation\n'
    printf '  %s init                                        asks what it needs\n' "$CLI"
    printf '\nIt writes %s/.env (mode 600) and store.json. You will need\n' "$DATA_HOME"
    printf '  · YMS_DB_NAME, SMB_HOST, SMB_SERVER_NAME, SMB_USER, SMB_PASSWORD\n'
    printf '  · SHOPIFY_VENDOR, SHOPIFY_STORE, SHOPIFY_ADMIN_TOKEN\n'
    printf '  · SHOPIFY_HANDLE_PREFIX  (choose once — see %s/.env.example)\n' "$REPO_DIR"
fi
printf '\nOr try it with no database at all:\n'
printf '  %s init --demo                                 the bundled example yard\n' "$CLI"
printf '  %s sync --sink csv --dry-run                   renders it, publishes nothing\n\n' "$CLI"
printf 'Then:\n'
printf '  %s doctor                                      check the installation\n' "$CLI"
printf '  %s status                                      what the pipeline believes\n' "$CLI"
printf '  %s --help                                      every command\n\n' "$CLI"
printf 'Docs: %shttps://github.com/Luxorium/coreyard%s\n\n' "$C_BOLD" "$C_RESET"
