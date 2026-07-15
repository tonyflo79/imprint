#!/usr/bin/env bash
set -euo pipefail
ARTIFACT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/imprint acceptance.XXXXXX")"
trap 'rm -rf "${TEST_ROOT}"' EXIT
export HOME="${TEST_ROOT}/Empty Home"
export XDG_CONFIG_HOME="${HOME}/Config Dir"
export XDG_DATA_HOME="${HOME}/Data Dir"
export IMPRINT_LAUNCHER_DIR="${HOME}/Command Bin"
export SHELL="${SHELL:-/bin/sh}"
INSTALL_ROOT="${HOME}/Applications/Imprint App"
CONFIG="${XDG_CONFIG_HOME}/imprint/config.json"
SETTINGS="${HOME}/.claude/settings.json"
DATA="${XDG_DATA_HOME}/imprint"
mkdir -p "${HOME}"

# A failed install must restore exact permissions on pre-existing external paths.
mkdir -p "$(dirname "${CONFIG}")" "${DATA}"
chmod 751 "$(dirname "${CONFIG}")"
chmod 750 "${DATA}"
config_parent_mode_before="$(stat -f '%Lp' "$(dirname "${CONFIG}")" 2>/dev/null || stat -c '%a' "$(dirname "${CONFIG}")")"
data_mode_before="$(stat -f '%Lp' "${DATA}" 2>/dev/null || stat -c '%a' "${DATA}")"

UNOWNED="${HOME}/Applications/Unowned App"
mkdir -p "${UNOWNED}"
printf '%s\n' 'must-survive' > "${UNOWNED}/sentinel.txt"
if bash "${ARTIFACT_ROOT}/install/uninstall.sh" --install-root "${UNOWNED}" --config "${CONFIG}" --settings "${SETTINGS}" >/dev/null 2>&1; then
  echo "Uninstaller accepted an unowned root" >&2
  exit 1
fi
test "$(cat "${UNOWNED}/sentinel.txt")" = "must-survive"

WHEEL="$(find "${ARTIFACT_ROOT}/dist" -type f -name 'imprint_local-3.0.1-*.whl' -print -quit)"
mv "${WHEEL}" "${WHEEL}.valid"
printf '%s\n' 'not-a-wheel' > "${WHEEL}"
if bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}" >/dev/null 2>&1; then
  echo "Installer accepted a corrupt wheel" >&2
  exit 1
fi
test ! -e "${INSTALL_ROOT}/.imprint-install-root"
test "$(stat -f '%Lp' "$(dirname "${CONFIG}")" 2>/dev/null || stat -c '%a' "$(dirname "${CONFIG}")")" = "${config_parent_mode_before}"
test "$(stat -f '%Lp' "${DATA}" 2>/dev/null || stat -c '%a' "${DATA}")" = "${data_mode_before}"
rm -f "${WHEEL}"
mv "${WHEEL}.valid" "${WHEEL}"

# Exercise the closed 3.0.0 ownership upgrade path before ordinary reinstall.
mkdir -p "${INSTALL_ROOT}"
printf '%s\n' legacy > "${INSTALL_ROOT}/legacy-owned.txt"
python3 "${ARTIFACT_ROOT}/tools/install/install_ownership.py" record --root "${INSTALL_ROOT}"
python3 - "${INSTALL_ROOT}/.imprint-owned-files.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["version"] = "3.0.0"
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
printf '%s\n' 'imprint-local:3.0.0' > "${INSTALL_ROOT}/.imprint-install-root"
bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}"
test ! -e "${INSTALL_ROOT}/legacy-owned.txt"
bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}"
test "$("${SHELL}" -lc 'imprint version')" = "3.0.1"
"${SHELL}" -lc 'imprint --help >/dev/null'
"${INSTALL_ROOT}/venv/bin/python" "${ARTIFACT_ROOT}/tests/acceptance/artifact_lifecycle.py" --data-root "${DATA}" --config "${CONFIG}"
health_rc=0
"${SHELL}" -lc 'imprint health >/dev/null' || health_rc=$?
test "${health_rc}" -eq 0 -o "${health_rc}" -eq 2
"${INSTALL_ROOT}/venv/bin/python" "${INSTALL_ROOT}/tools/manage_hooks.py" status --settings "${SETTINGS}" --python "${INSTALL_ROOT}/venv/bin/python" --hooks-dir "${INSTALL_ROOT}/hooks"
printf '%s\n' 'unowned' > "${INSTALL_ROOT}/unowned-sentinel.txt"
if bash "${ARTIFACT_ROOT}/install/uninstall.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" >/dev/null 2>&1; then
  echo "Uninstaller removed a root containing an unowned file" >&2
  exit 1
fi
test -f "${INSTALL_ROOT}/unowned-sentinel.txt"
grep -q 'imprint-local-managed-hook' "${SETTINGS}"
rm -f "${INSTALL_ROOT}/unowned-sentinel.txt"
bash "${ARTIFACT_ROOT}/install/uninstall.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}"
test ! -e "${INSTALL_ROOT}"
test ! -e "${IMPRINT_LAUNCHER_DIR}/imprint"
if [ -f "${HOME}/.zprofile" ]; then ! grep -q 'imprint-local-owned-path' "${HOME}/.zprofile"; fi
if [ -f "${HOME}/.bash_profile" ]; then ! grep -q 'imprint-local-owned-path' "${HOME}/.bash_profile"; fi
if [ -f "${HOME}/.profile" ]; then ! grep -q 'imprint-local-owned-path' "${HOME}/.profile"; fi
test -f "${DATA}/default/acceptance-data-sentinel.txt"
python3 - "${SETTINGS}" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert "imprint-local-managed-hook" not in json.dumps(value)
PY
bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}"
printf '%s\n' '#!/bin/sh' 'echo unowned' > "${IMPRINT_LAUNCHER_DIR}/imprint"
chmod +x "${IMPRINT_LAUNCHER_DIR}/imprint"
bash "${ARTIFACT_ROOT}/install/uninstall.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --purge-config
test "$("${IMPRINT_LAUNCHER_DIR}/imprint")" = "unowned"
rm -f "${IMPRINT_LAUNCHER_DIR}/imprint"
echo "artifact lifecycle: PASS"
