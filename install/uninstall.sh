#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${IMPRINT_INSTALL_ROOT:-${HOME}/.local/lib/imprint-local}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-${HOME}/.claude/settings.json}"
CONFIG_PATH="${IMPRINT_CONFIG:-${XDG_CONFIG_HOME:-${HOME}/.config}/imprint/config.json}"
LAUNCHER_DIR="${IMPRINT_LAUNCHER_DIR:-${HOME}/.local/bin}"
PURGE_CONFIG=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --settings) SETTINGS_PATH="$2"; shift 2 ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --launcher-dir) LAUNCHER_DIR="$2"; shift 2 ;;
    --purge-config) PURGE_CONFIG=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${INSTALL_ROOT}" ] || [ "${INSTALL_ROOT}" = "/" ] || [ "${INSTALL_ROOT}" = "${HOME}" ] || [ -L "${INSTALL_ROOT}" ]; then
  echo "Refusing an unsafe install root: ${INSTALL_ROOT}" >&2
  exit 2
fi
MARKER="${INSTALL_ROOT}/.imprint-install-root"
if [ ! -f "${MARKER}" ] || [ "$(cat "${MARKER}")" != "imprint-local:3.0.1" ]; then
  echo "Refusing to remove an install root without Imprint's ownership marker: ${INSTALL_ROOT}" >&2
  exit 2
fi
PYTHON="${INSTALL_ROOT}/venv/bin/python"
OWNERSHIP="${INSTALL_ROOT}/tools/install_ownership.py"
MANAGER="${INSTALL_ROOT}/tools/manage_hooks.py"
LAUNCHER_PATH="${LAUNCHER_DIR}/imprint"
PROFILE_RECORD="${INSTALL_ROOT}/.imprint-shell-profile"
if [ ! -x "${PYTHON}" ] || [ ! -f "${OWNERSHIP}" ]; then
  echo "Refusing uninstall because ownership tooling is missing." >&2
  exit 2
fi
"${PYTHON}" "${OWNERSHIP}" verify --root "${INSTALL_ROOT}"
if [ -f "${MANAGER}" ]; then
  "${PYTHON}" "${MANAGER}" unregister --settings "${SETTINGS_PATH}" --python "${PYTHON}" --hooks-dir "${INSTALL_ROOT}/hooks"
fi
if [ -f "${PROFILE_RECORD}" ]; then
  SHELL_PROFILE="$(cat "${PROFILE_RECORD}")"
  "${PYTHON}" - "${SHELL_PROFILE}" "${LAUNCHER_DIR}" <<'PY'
import shlex, sys
from pathlib import Path
path, launcher_dir = Path(sys.argv[1]), sys.argv[2]
if path.exists() and path.is_file() and not path.is_symlink():
    start = "# >>> imprint-local-owned-path:3.0.1 >>>"
    end = "# <<< imprint-local-owned-path:3.0.1 <<<"
    block = f'{start}\nexport PATH={shlex.quote(launcher_dir)}:"$PATH"\n{end}\n'
    prior = path.read_text(encoding="utf-8")
    if block in prior:
        path.write_text(prior.replace(block, "", 1), encoding="utf-8")
PY
fi
if [ -f "${LAUNCHER_PATH}" ] && ! [ -L "${LAUNCHER_PATH}" ] \
  && grep -Fx '# imprint-local-owned-launcher:3.0.1' "${LAUNCHER_PATH}" >/dev/null \
  && grep -F "${INSTALL_ROOT}/venv/bin/imprint" "${LAUNCHER_PATH}" >/dev/null; then
  rm -f -- "${LAUNCHER_PATH}"
elif [ -e "${LAUNCHER_PATH}" ] || [ -L "${LAUNCHER_PATH}" ]; then
  echo "Leaving unowned or modified launcher intact: ${LAUNCHER_PATH}" >&2
fi
"${PYTHON}" "${OWNERSHIP}" uninstall --root "${INSTALL_ROOT}"
if [ "${PURGE_CONFIG}" -eq 1 ]; then rm -f -- "${CONFIG_PATH}"; fi
echo "Imprint code and managed hooks removed. Captured data was preserved."
