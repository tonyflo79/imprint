#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_ROOT="${IMPRINT_INSTALL_ROOT:-${HOME}/.local/lib/imprint-local}"
CONFIG_PATH="${IMPRINT_CONFIG:-${XDG_CONFIG_HOME:-${HOME}/.config}/imprint/config.json}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-${HOME}/.claude/settings.json}"
DATA_ROOT="${IMPRINT_DATA_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/imprint}"
OPERATOR="default"
REGISTER_HOOKS=1
PYTHON="${PYTHON:-python3}"
SUCCESS=0
BACKUP_ROOT=""
STATE_ROOT=""

usage() {
  echo "Usage: install.sh [--install-root PATH] [--config PATH] [--settings PATH] [--data-root PATH] [--operator SLUG] [--python PATH] [--no-hooks]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --settings) SETTINGS_PATH="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --operator) OPERATOR="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --no-hooks) REGISTER_HOOKS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

"${PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Imprint requires Python 3.10+")'
case "${OPERATOR}" in *[!a-z0-9-]*|'') echo "Operator must use lowercase letters, digits, and hyphens." >&2; exit 2 ;; esac

INSTALL_ROOT="$(${PYTHON} - "${INSTALL_ROOT}" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
HOME_ROOT="$(cd "${HOME}" && pwd -P)"
if [ "${INSTALL_ROOT}" = "/" ] || [ "${INSTALL_ROOT}" = "${HOME_ROOT}" ] || [ -L "${INSTALL_ROOT}" ]; then
  echo "Refusing an unsafe install root: ${INSTALL_ROOT}" >&2
  exit 2
fi
MARKER="${INSTALL_ROOT}/.imprint-install-root"
if [ -d "${INSTALL_ROOT}" ] && [ -n "$(find "${INSTALL_ROOT}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  if [ ! -f "${MARKER}" ] || [ "$(cat "${MARKER}")" != "imprint-local:3.0.0" ]; then
    echo "Refusing a non-empty install root not owned by Imprint: ${INSTALL_ROOT}" >&2
    exit 2
  fi
fi

WHEEL="$(find "${ARTIFACT_ROOT}/dist" -maxdepth 1 -type f -name 'imprint_local-3.0.0-*.whl' -print -quit)"
if [ -z "${WHEEL}" ]; then
  echo "The release artifact is incomplete: dist/imprint_local-3.0.0-*.whl is missing." >&2
  exit 2
fi

STATE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/imprint-install-state.XXXXXX")"
BACKUP_ROOT="${INSTALL_ROOT}.imprint-backup.$$"
if [ -e "${BACKUP_ROOT}" ] || [ -L "${BACKUP_ROOT}" ]; then
  echo "Refusing to overwrite stale install backup: ${BACKUP_ROOT}" >&2
  exit 2
fi

snapshot_file() {
  local source="$1" name="$2"
  if [ -f "${source}" ]; then cp -p "${source}" "${STATE_ROOT}/${name}"; else : > "${STATE_ROOT}/${name}.absent"; fi
}
restore_file() {
  local destination="$1" name="$2"
  if [ -f "${STATE_ROOT}/${name}.absent" ]; then rm -f -- "${destination}"; else mkdir -p "$(dirname "${destination}")"; cp -p "${STATE_ROOT}/${name}" "${destination}"; fi
}
remove_new_root() {
  if [ -d "${INSTALL_ROOT}" ] && [ ! -L "${INSTALL_ROOT}" ]; then
    "${PYTHON}" - "${INSTALL_ROOT}" <<'PY'
import shutil, sys
from pathlib import Path
root = Path(sys.argv[1])
if root == Path(root.anchor) or root == Path.home().resolve() or root.is_symlink():
    raise SystemExit("unsafe rollback root")
shutil.rmtree(root)
PY
  fi
}
rollback() {
  local status=$?
  if [ "${SUCCESS}" -ne 1 ]; then
    remove_new_root || true
    if [ -d "${BACKUP_ROOT}" ]; then mv "${BACKUP_ROOT}" "${INSTALL_ROOT}"; fi
    restore_file "${CONFIG_PATH}" config || true
    restore_file "${SETTINGS_PATH}" settings || true
  fi
  [ -n "${STATE_ROOT}" ] && rm -rf -- "${STATE_ROOT}"
  exit "${status}"
}
snapshot_file "${CONFIG_PATH}" config
snapshot_file "${SETTINGS_PATH}" settings
trap rollback EXIT

if [ -d "${INSTALL_ROOT}" ]; then mv "${INSTALL_ROOT}" "${BACKUP_ROOT}"; fi
mkdir -p "${INSTALL_ROOT}" "$(dirname "${CONFIG_PATH}")" "${DATA_ROOT}"
"${PYTHON}" -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/python" -m pip install --disable-pip-version-check --no-index --force-reinstall "${WHEEL}"
cp -R "${ARTIFACT_ROOT}/hooks" "${INSTALL_ROOT}/hooks"
mkdir -p "${INSTALL_ROOT}/tools"
cp "${ARTIFACT_ROOT}/tools/install/manage_hooks.py" "${INSTALL_ROOT}/tools/manage_hooks.py"
cp "${ARTIFACT_ROOT}/tools/install/install_ownership.py" "${INSTALL_ROOT}/tools/install_ownership.py"

"${INSTALL_ROOT}/venv/bin/python" - "${CONFIG_PATH}" "${DATA_ROOT}" "${OPERATOR}" "${INSTALL_ROOT}/hooks" <<'PY'
import json, os, sys
from pathlib import Path
path, root, operator, hooks_dir = Path(sys.argv[1]), str(Path(sys.argv[2]).expanduser().resolve()), sys.argv[3], str(Path(sys.argv[4]).resolve())
value = {}
if path.exists():
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise SystemExit("Existing config must contain a JSON object")
value.update({"config_version":"3.0.0", "data_root":root, "operator_slug":operator, "hooks_dir":hooks_dir})
value.setdefault("node_id", "primary")
value.setdefault("compiler", True)
value.setdefault("context_budget_bytes", 32768)
value.setdefault("experimental", {"digest":False, "profile_learning":False})
tmp = path.with_suffix(path.suffix + ".imprint-tmp")
tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY

if [ "${REGISTER_HOOKS}" -eq 1 ]; then
  "${INSTALL_ROOT}/venv/bin/python" "${INSTALL_ROOT}/tools/manage_hooks.py" register \
    --settings "${SETTINGS_PATH}" --python "${INSTALL_ROOT}/venv/bin/python" --hooks-dir "${INSTALL_ROOT}/hooks"
fi
IMPRINT_CONFIG="${CONFIG_PATH}" "${INSTALL_ROOT}/venv/bin/imprint" version | grep -Fx '3.0.0' >/dev/null
"${INSTALL_ROOT}/venv/bin/python" "${INSTALL_ROOT}/tools/install_ownership.py" record --root "${INSTALL_ROOT}"
if [ -d "${BACKUP_ROOT}" ]; then
  "${PYTHON}" "${INSTALL_ROOT}/tools/install_ownership.py" uninstall --root "${BACKUP_ROOT}"
fi
printf '%s\n' 'imprint-local:3.0.0' > "${MARKER}.tmp"
mv "${MARKER}.tmp" "${MARKER}"
SUCCESS=1
rm -rf -- "${STATE_ROOT}"
STATE_ROOT=""
trap - EXIT
echo "Imprint 3.0.0 installed. Data root: ${DATA_ROOT}. No telemetry is enabled."
