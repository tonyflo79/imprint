#!/usr/bin/env bash
set -euo pipefail
ARTIFACT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/imprint acceptance.XXXXXX")"
trap 'rm -rf "${TEST_ROOT}"' EXIT
export HOME="${TEST_ROOT}/Empty Home"
export XDG_CONFIG_HOME="${HOME}/Config Dir"
export XDG_DATA_HOME="${HOME}/Data Dir"
INSTALL_ROOT="${HOME}/Applications/Imprint App"
CONFIG="${XDG_CONFIG_HOME}/imprint/config.json"
SETTINGS="${HOME}/.claude/settings.json"
DATA="${XDG_DATA_HOME}/imprint"
mkdir -p "${HOME}"

UNOWNED="${HOME}/Applications/Unowned App"
mkdir -p "${UNOWNED}"
printf '%s\n' 'must-survive' > "${UNOWNED}/sentinel.txt"
if sh "${ARTIFACT_ROOT}/install/uninstall.sh" --install-root "${UNOWNED}" --config "${CONFIG}" --settings "${SETTINGS}" >/dev/null 2>&1; then
  echo "Uninstaller accepted an unowned root" >&2
  exit 1
fi
test "$(cat "${UNOWNED}/sentinel.txt")" = "must-survive"

WHEEL="$(find "${ARTIFACT_ROOT}/dist" -type f -name 'imprint_local-3.0.0-*.whl' -print -quit)"
mv "${WHEEL}" "${WHEEL}.valid"
printf '%s\n' 'not-a-wheel' > "${WHEEL}"
if bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}" >/dev/null 2>&1; then
  echo "Installer accepted a corrupt wheel" >&2
  exit 1
fi
test ! -e "${INSTALL_ROOT}/.imprint-install-root"
rm -f "${WHEEL}"
mv "${WHEEL}.valid" "${WHEEL}"

bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}"
bash "${ARTIFACT_ROOT}/install/install.sh" --install-root "${INSTALL_ROOT}" --config "${CONFIG}" --settings "${SETTINGS}" --data-root "${DATA}"
"${INSTALL_ROOT}/venv/bin/python" "${ARTIFACT_ROOT}/tests/acceptance/artifact_lifecycle.py" --data-root "${DATA}" --config "${CONFIG}"
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
test -f "${DATA}/default/acceptance-data-sentinel.txt"
python3 - "${SETTINGS}" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert "imprint-local-managed-hook" not in json.dumps(value)
PY
echo "artifact lifecycle: PASS"
