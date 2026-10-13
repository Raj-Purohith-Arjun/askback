#!/usr/bin/env bash
# One-time setup for macOS and Linux. Creates a private .venv in this folder
# and installs the project into it. Nothing is installed system-wide.
set -euo pipefail
cd "$(dirname "$0")"

echo
echo " askback - setup"
echo " ================="
echo

# --- 1. Find a Python that is new enough ----------------------------------- #
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo " [X] No Python 3.11 or newer found."
    echo
    echo "     macOS:  brew install python@3.12"
    echo "     Ubuntu: sudo apt install python3 python3-venv"
    echo "     Or:     https://www.python.org/downloads/"
    echo
    exit 1
fi
echo " [1/3] Using $PYTHON ($("$PYTHON" -c 'import sys;print(sys.version.split()[0])'))."

# --- 2. Virtual environment ------------------------------------------------- #
if [ -x ".venv/bin/python" ]; then
    echo " [2/3] Reusing the existing .venv folder."
else
    echo " [2/3] Creating .venv ..."
    "$PYTHON" -m venv .venv
fi

# --- 3. Dependencies -------------------------------------------------------- #
echo " [3/3] Installing packages (needs internet, takes 1-3 minutes) ..."
if ! .venv/bin/python -m pip install --quiet --upgrade pip ||
   ! .venv/bin/python -m pip install --quiet -c constraints.txt -e ".[all]"; then
    echo
    echo " [X] Install failed. Usually no internet access or a proxy."
    echo "     Behind a proxy? export HTTPS_PROXY=http://your-proxy:port"
    echo "     then run this script again."
    echo
    exit 1
fi

echo
echo " Setup complete."
echo
echo "   bash verify.sh   check everything works"
echo "   bash start.sh    launch the dashboard for the HR example at http://localhost:8000"
echo
