#!/usr/bin/env bash
set -euo pipefail

for dependency in git python3; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf '缺少必要程序，请先安装：%s\n' "$dependency" >&2
        exit 1
    fi
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10 或更高版本")'

delegate_tmp="$(mktemp -d "${TMPDIR:-/tmp}/delegate-workers-install.XXXXXX")"
trap 'rm -rf -- "$delegate_tmp"' EXIT
git clone --quiet --depth 1 --branch main -- \
    https://github.com/haobanz/codex-delegate-workers.git "$delegate_tmp/repository"

if [ "$#" -eq 0 ]; then
    set -- install
fi
python3 "$delegate_tmp/repository/skills/delegate-workers/scripts/manage.py" \
    --source "$delegate_tmp/repository" "$@"
