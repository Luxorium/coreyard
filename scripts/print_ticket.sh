#!/usr/bin/env bash
# Render a CoreYard pull ticket to PDF and send it to a printer.
#
#   scripts/print_ticket.sh out/tickets/order-1042.html
#
# Wire it into the order receiver by pointing COREYARD_PRINT_CMD at it in .env:
#
#   COREYARD_PRINT_CMD=scripts/print_ticket.sh {file}
#
# Why the PDF step: the ticket is HTML, and CUPS dropped its HTML filters years ago, so
# `lp file.html` prints the markup as plain text on a modern system rather than the page.
# Headless Chromium is used purely as a renderer — it honours the ticket's print CSS, which
# is already sized for US Letter, so what comes out matches what a browser would print.
#
# Environment:
#   COREYARD_PRINTER   CUPS queue name (default: the system default printer)
#   COREYARD_BROWSER   browser binary to render with (default: first one found)
set -euo pipefail

file="${1:-}"
if [ -z "$file" ] || [ ! -r "$file" ]; then
    echo "print_ticket: no readable ticket at '${file}'" >&2
    exit 2
fi

browser="${COREYARD_BROWSER:-}"
if [ -z "$browser" ]; then
    for candidate in chromium chromium-browser google-chrome google-chrome-stable brave-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then browser="$candidate"; break; fi
    done
fi
if [ -z "$browser" ]; then
    echo "print_ticket: no Chromium/Chrome found to render the ticket." >&2
    echo "  Install one, or set COREYARD_BROWSER to a binary that supports --print-to-pdf." >&2
    exit 3
fi
if ! command -v lp >/dev/null 2>&1; then
    echo "print_ticket: CUPS 'lp' command not found." >&2
    echo "  Install your distro's CUPS client package before enabling ticket printing." >&2
    exit 3
fi

# A private profile dir per run: a shared one makes two tickets printing at once fight over
# the same lock, and an order webhook can genuinely deliver two orders in the same second.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
pdf="$work/ticket.pdf"

browser_args=(
    --headless
    --disable-gpu
    --user-data-dir="$work/profile"
    --no-pdf-header-footer
    --print-to-pdf="$pdf"
)
# Chromium refuses to start as root with its sandbox enabled. Normal user services retain
# the sandbox; only an explicitly root-run receiver gets the compatibility flag.
if [ "$(id -u)" -eq 0 ]; then
    browser_args+=(--no-sandbox)
fi
"$browser" "${browser_args[@]}" "file://$(readlink -f "$file")" >/dev/null 2>&1

if [ ! -s "$pdf" ]; then
    echo "print_ticket: $browser produced no PDF for '$file'" >&2
    exit 4
fi

if [ -n "${COREYARD_PRINTER:-}" ]; then
    lp -d "$COREYARD_PRINTER" -o media=Letter -- "$pdf"
else
    lp -o media=Letter -- "$pdf"
fi
