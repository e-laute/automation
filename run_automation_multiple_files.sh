#!/bin/bash
set -euo pipefail

# The work packages definition is hardcoded here rather than passed in as an
# input, since caller_multiple_files.yml no longer takes a workpackage_json argument.
WORKPACKAGES_ADRESS="central_repo/work_packages.json"

# Install Python dependencies from the central repo
python -m pip install --upgrade pip
pip install -r "$GITHUB_WORKSPACE/central-repo/scripts/GitHub_Actions/requirements-dispatch.txt"

# Build the coordinator command. Optional flags (-ft, -e, -a) are only added
# when the corresponding env var is non-empty.
cmd=(python "$GITHUB_WORKSPACE/central-repo/scripts/GitHub_Actions/find_files_wrapper.py" \
    -w "$workpackage_id" \
    -wa "$WORKPACKAGES_ADRESS")

[ -n "${filetype:-}" ] && cmd+=(-ft "$filetype")
[ -n "${exclude:-}" ]  && cmd+=(-e "$exclude")
[ -n "${addargs:-}" ]  && cmd+=(-a "$addargs")

"${cmd[@]}"
