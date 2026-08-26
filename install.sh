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
#   ./install.sh --help
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

SKIP_DEPS=0
ASSUME_YES=0

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

for arg in "$@"; do
    case "$arg" in
        --no-deps) SKIP_DEPS=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --help|-h) usage ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

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

# ------------------------------------------------------------------ env -----
step "Configuring"
if [ -f "$REPO_DIR/.env" ]; then
    ok ".env already exists — left untouched"
else
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    ok "created .env from .env.example (mode 600)"
    NEEDS_CONFIG=1
fi

# Deliberately NOT auto-copied: a schema.json full of PLACEHOLDERs would look configured
# and then fail with a confusing SQL error. Absent, the CLI prints a clear next step.
if [ -f "$REPO_DIR/schema.json" ]; then
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
# Resolve through symlinks before locating the repo. The installer tells you to put this on
# your PATH with `ln -s`, and BASH_SOURCE is then the *link* (~/.local/bin/coreyard), so
# taking its dirname looked for the virtualenv in ~/.local and reported it missing. The loop
# rather than `readlink -f` because it also handles a chain of links, and relative ones.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    LINKDIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    case "$SOURCE" in /*) ;; *) SOURCE="$LINKDIR/$SOURCE" ;; esac
done
HERE="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || { echo "coreyard: virtualenv missing — run ./install.sh" >&2; exit 1; }
cd "$HERE"
# -u: scheduled runs redirect into a log file, where block buffering would hold a job's
# output back for minutes. `coreyard doctor` decides the order poller is alive by watching
# orders.log grow, so a buffered log is indistinguishable from a dead poller.
exec "$PY" -u -m coreyard "$@"
LAUNCHER
chmod +x "$REPO_DIR/bin/coreyard"
ok "installed bin/coreyard launcher"

# ---------------------------------------------------------------- verify ----
step "Verifying the install"
if (cd "$REPO_DIR" && "$VENV_PY" -m unittest discover -s tests >/dev/null 2>&1); then
    ok "offline test suite passed"
else
    die "the test suite failed — the install is not healthy"
fi

# ------------------------------------------------------------------ done ----
printf '\n%sCoreYard is installed.%s\n\n' "$C_GREEN$C_BOLD" "$C_RESET"
if [ "${NEEDS_SCHEMA:-0}" -eq 1 ]; then
    printf 'First: map your database\n'
    printf '  cp schema.example.json schema.json\n'
    printf '  .venv/bin/python -m coreyard.yms.discover_schema   # lists your tables\n'
    printf '  then replace each PLACEHOLDER in schema.json\n\n'
fi
if [ "${NEEDS_CONFIG:-0}" -eq 1 ]; then
    printf 'Next: edit %s.env%s and fill in\n' "$C_BOLD" "$C_RESET"
    printf '  · YMS_DB_NAME, SMB_HOST, SMB_SERVER_NAME, SMB_USER, SMB_PASSWORD\n'
    printf '  · SHOPIFY_VENDOR, SHOPIFY_STORE, SHOPIFY_ADMIN_TOKEN\n'
    printf '  · SHOPIFY_HANDLE_PREFIX  (choose once — see the comment in .env)\n\n'
fi
printf 'Then:\n'
printf '  bin/coreyard doctor                           check the installation\n'
printf '  bin/coreyard status                           what the pipeline believes\n'
printf '  bin/coreyard sync --dry-run                   what a sync would do\n'
printf '  bin/coreyard --help                           every command\n\n'
printf 'Tip: add it to your PATH with\n'
printf '  ln -s "%s/bin/coreyard" ~/.local/bin/coreyard\n\n' "$REPO_DIR"
printf 'Docs: %shttps://github.com/Luxorium/coreyard%s\n\n' "$C_BOLD" "$C_RESET"
