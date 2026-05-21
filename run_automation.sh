#!/bin/bash
set -euo pipefail

# Install Python dependencies from the central repo
python -m pip install --upgrade pip
pip install -r "$GITHUB_WORKSPACE/central-repo/scripts/requirements.txt"

# Run the coordinator script from the central repo on the caller repo's data.
# Pass -p only when parameters is non-empty.
if [ -n "${parameters:-}" ]; then
    python "$GITHUB_WORKSPACE/central-repo/scripts/coordinator.py" \
        -id "$workpackage_id" \
        -wp "$workpackage_json" \
        -f "caller-repo/$filepath" \
        -p "$parameters"
else
    python "$GITHUB_WORKSPACE/central-repo/scripts/coordinator.py" \
        -id "$workpackage_id" \
        -wp "$workpackage_json" \
        -f "caller-repo/$filepath"
fi
