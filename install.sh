#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$(realpath "$0")")" && pwd)
PREFIX=${PREFIX:-${HOME}/.local}
BIN_DIR="${PREFIX}/bin"
LIB_DIR="${PREFIX}/lib/serena-shared"

mode=symlink
while [[ $# -gt 0 ]]; do
  case "$1" in
    --symlink)
      mode=symlink
      shift
      ;;
    --copy)
      mode=copy
      shift
      ;;
    --uninstall)
      mode=uninstall
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage: ./install.sh [--symlink | --copy | --uninstall]

Options:
  --symlink    Symlink into \$PREFIX (default: ~/.local)
  --copy       Copy files into \$PREFIX instead of symlinking
  --uninstall  Remove installed files/symlinks from \$PREFIX
EOF
      exit 0
      ;;
    *)
      echo "install.sh: unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$mode" == "uninstall" ]]; then
  rm -f "${BIN_DIR}/serena-shared" "${LIB_DIR}/proxy.py" "${LIB_DIR}/reaper.py"
  rmdir "${LIB_DIR}" 2>/dev/null || true
  echo "Uninstalled serena-shared from ${PREFIX}"
  exit 0
fi

mkdir -p "${BIN_DIR}" "${LIB_DIR}"
chmod +x "${REPO_DIR}/bin/serena-shared" "${REPO_DIR}/lib/proxy.py"

if [[ "$mode" == "symlink" ]]; then
  ln -sfn "${REPO_DIR}/bin/serena-shared" "${BIN_DIR}/serena-shared"
  ln -sfn "${REPO_DIR}/lib/proxy.py" "${LIB_DIR}/proxy.py"
  ln -sfn "${REPO_DIR}/lib/reaper.py" "${LIB_DIR}/reaper.py"
  echo "Symlinked serena-shared into ${PREFIX}"
else
  install -m 0755 "${REPO_DIR}/bin/serena-shared" "${BIN_DIR}/serena-shared"
  install -m 0755 "${REPO_DIR}/lib/proxy.py" "${LIB_DIR}/proxy.py"
  install -m 0644 "${REPO_DIR}/lib/reaper.py" "${LIB_DIR}/reaper.py"
  echo "Copied serena-shared into ${PREFIX}"
fi
