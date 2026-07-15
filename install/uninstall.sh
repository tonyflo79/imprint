#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${IMPRINT_INSTALL_ROOT:-${HOME}/.local/lib/imprint-local}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-${HOME}/.claude/settings.json}"
CONFIG_PATH="${IMPRINT_CONFIG:-${XDG_CONFIG_HOME:-${HOME}/.config}/imprint/config.json}"
PURGE_CONFIG=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --settings) SETTINGS_PATH="$2"; shift 2 ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --purge-config) PURGE_CONFIG=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${INSTALL_ROOT}" ] || [ "${INSTALL_ROOT}" = "/" ] || [ "${INSTALL_ROOT}" = "${HOME}" ] || [ -L "${INSTALL_ROOT}" ]; then
  echo "Refusing an unsafe install root: ${INSTALL_ROOT}" >&2
  exit 2
fi
MARKER="${INSTALL_ROOT}/.imprint-install-root"
if [ ! -f "${MARKER}" ] || [ "$(cat "${MARKER}")" != "imprint-local:3.0.0" ]; then
  echo "Refusing to remove an install root without Imprint's ownership marker: ${INSTALL_ROOT}" >&2
  exit 2
fi
PYTHON="${INSTALL_ROOT}/venv/bin/python"
OWNERSHIP="${INSTALL_ROOT}/tools/install_ownership.py"
MANAGER="${INSTALL_ROOT}/tools/manage_hooks.py"
if [ ! -x "${PYTHON}" ] || [ ! -f "${OWNERSHIP}" ]; then
  echo "Refusing uninstall because ownership tooling is missing." >&2
  exit 2
fi
"${PYTHON}" "${OWNERSHIP}" verify --root "${INSTALL_ROOT}"
if [ -f "${MANAGER}" ]; then
  "${PYTHON}" "${MANAGER}" unregister --settings "${SETTINGS_PATH}" --python "${PYTHON}" --hooks-dir "${INSTALL_ROOT}/hooks"
fi
"${PYTHON}" "${OWNERSHIP}" uninstall --root "${INSTALL_ROOT}"
if [ "${PURGE_CONFIG}" -eq 1 ]; then rm -f -- "${CONFIG_PATH}"; fi
echo "Imprint code and managed hooks removed. Captured data was preserved."
