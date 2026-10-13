#!/usr/bin/env bash
# Prove the install works: unit tests, the built-in example suite, then the HR example.
set -uo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo " [X] Not set up yet. Run: bash setup.sh"
    exit 1
fi

echo
echo " 1. Unit tests"
echo " -------------"
if ! .venv/bin/python -m pytest -q -p no:warnings; then
    echo
    echo " [X] Unit tests failed. Something is wrong with the install."
    exit 1
fi

echo
echo " 2. The built-in example suite (must pass)"
echo " -----------------------------------------"
if ! .venv/bin/python -m askback eval --quiet --format json,html --name verify; then
    echo
    echo " [X] The built-in example suite failed. It is meant to pass on a working install."
    exit 1
fi

echo
echo " 3. The HR example (red on purpose)"
echo " ----------------------------------"
.venv/bin/python -m askback --config examples/hr/config.yaml eval --quiet --format json,html --name verify-hr
code=$?
if [ "$code" -ne 1 ]; then
    echo
    echo " [!] Expected the HR example to exit 1 (it grades a simulator with known flaws),"
    echo "     but it exited $code. See examples/hr/README.md."
fi

echo
echo " What you should see:"
echo "   1. every test passes;"
echo "   2. the example suite passes, so the tool works;"
echo "   3. the HR example FAILS some cases and exits 1. That is the tool catching the"
echo "      simulator's real flaws, not a broken install: examples/hr/README.md says which."
echo
echo " Reports: reports/verify.html (example suite), reports/verify-hr.html (HR example)"
echo
