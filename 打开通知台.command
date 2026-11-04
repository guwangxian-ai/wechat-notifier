#!/bin/zsh
set -eu
task_root="${0:A:h}"
cd "$task_root"
exec "$task_root/.venv/bin/python" "$task_root/scripts/open_dashboard.py"
