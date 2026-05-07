# E-LAUTE GitHub Actions — Central Repository

This is the **central repository** for the [E-LAUTE digital edition](https://e-laute.info) automation setup.
It contains the automation entry-point shell script and Python processing scripts that operate on MEI files stored in the project's many caller repositories.

---

## Overview of the automation architecture

E-LAUTE's automation follows the **central / caller** pattern defined by the mei-friend automation setup (documented in detail [in the mei-friend docs](https://mei-friend.github.io/docs/advanced/automation/)).
The three components are:

```
mei-friend (browser)
      │
      │  triggers via GitHub API (workflow_dispatch)
      ▼
Caller repository               ← one per source/manuscript, contains MEI files
  .github/workflows/caller.yml  ← generic relay workflow
      │
      │  checks out central repo, runs scripts/automation/run_automation.sh
      ▼
This repository  (e-laute/automation)   ← you are here
  scripts/automation/run_automation.sh  ← entry point: sets up Python environment and runs coordinator.py
  scripts/                              ← Python processing scripts
      │
      ▼
Caller repository               ← results committed back here
```

When a user selects a work package in mei-friend and clicks "Run workflow", the event travels:

1. mei-friend → caller repository (via GitHub API `workflow_dispatch`)
2. The caller workflow checks out this central repository and runs `scripts/automation/run_automation.sh`, passing the dispatch inputs (work package, file path, parameters).
3. The automation script runs the coordinator against the caller repository's data and commits any changes back.

---

## How the coordinator works

The coordinator ([scripts/coordinator.py](scripts/coordinator.py)) is the engine behind all work package processing.
When invoked, it performs the following steps:

### 1. Parse the target file and build the active DOM

The file at the given path is parsed into an XML tree with `lxml`.
The resulting element tree is wrapped in a metadata dictionary:

```python
{
    "filename": "A-Wn_Cod._9704_n07_8r-8v_enc_dipl_GLT",  # stem of the file
    "dom":      <lxml Element>,                              # parsed XML root
    "notationtype": "dipl_GLT",                             # derived from filename
}
```

The notation type is extracted from the filename following E-LAUTE's naming convention:
`…_enc_(dipl|ed)_(GLT|FLT|ILT|CMN).mei`

This is the **active DOM** — the file being processed.

### 2. Collect context DOMs

The coordinator also parses every other `.mei` file in the same directory and wraps each one in the same metadata structure. These are the **context DOMs** — sibling encodings of the same piece (e.g. the diplomatic GLT alongside the edited CMN of the same piece).

Scripts can look up a specific sibling by its notation type (e.g. `"dipl_GLT"`) to copy elements across files, check consistency, or derive content.

### 3. Run the scripts in sequence

The work package definition specifies a list of scripts to run, for example:

```json
"scripts": [
    "script_collection.add_header_from_context",
    "script_collection.add_facs_from_context",
    "script_collection.add_section_foldir_from_context_to_ed",
    "script_collection.add_finis_to_last_measure",
    "script_collection.add_sbs_every_n"
]
```

Each script receives the current state of the active DOM, the list of context DOMs, and any user-supplied parameters.
It returns the (possibly modified) active DOM plus optional output messages.
The output of each script is passed as input to the next — this is what makes multi-step processing possible.

If any script raises a `RuntimeError`, execution stops immediately and no file is written.

### 4. Write back (if commitResult is true)

Once all scripts have run successfully, the coordinator writes the modified XML back to the original file and appends a provenance entry to the MEI `<appInfo>` block recording that the GitHub Actions scripts were used.
The surrounding GitHub Actions workflow then commits this change back to the caller repository.

---

## Work packages

Work packages are defined in [scripts/work_packages.json](scripts/work_packages.json).
Each entry describes one named operation selectable in mei-friend:

```json
{
  "id": "prepare_ed_GLT_from_fronimo",
  "label": "Prepare ed_GLT from Fronimo",
  "description": "Add Header, Facs, Section, Finis, sbs.",
  "userFacing": true,
  "params": {
    "getElemFrom": { "type": "String", "default": "dipl_GLT" },
    "sbInterval": { "type": "Number", "default": 5 },
    "projectstaff": { "type": "String" },
    "finisText": { "type": "String", "default": "" }
  },
  "scripts": [
    "script_collection.add_header_from_context",
    "script_collection.add_facs_from_context",
    "script_collection.add_section_foldir_from_context_to_ed",
    "script_collection.add_finis_to_last_measure",
    "script_collection.add_sbs_every_n"
  ],
  "commitResult": true
}
```

| Field          | Purpose                                                                                                  |
| -------------- | -------------------------------------------------------------------------------------------------------- |
| `id`           | Internal identifier, used as `workpackage_id` when triggering the workflow                               |
| `label`        | Display name shown in mei-friend                                                                         |
| `description`  | Tooltip shown in mei-friend                                                                              |
| `userFacing`   | Whether to show this work package in the mei-friend dropdown                                             |
| `params`       | Parameters the user can fill in; each may have a `default` value and a `type` (`"String"` or `"Number"`) |
| `scripts`      | Ordered list of `module.function` paths to call, executed in sequence                                    |
| `commitResult` | Whether to write the result back to the file and commit it                                               |

### Available work packages

| ID                             | What it does                                                                                       |
| ------------------------------ | -------------------------------------------------------------------------------------------------- |
| `add_sbs`                      | Adds `<sb/>` every n measures                                                                      |
| `remove_and_add_sbs`           | Removes all `<sb/>` and re-adds them every n measures                                              |
| `add_facs`                     | Copies the `<facsimile>` block from a sibling file                                                 |
| `add_header_from_context`      | Copies the `<meiHead>` from a sibling file with appropriate adjustments                            |
| `add_section_and_foldir_to_ed` | Adds page-based `<section>` structure and folio `<dir>` annotations from a sibling diplomatic file |
| `add_finis`                    | Adds a finis text at the end of the last measure                                                   |
| `prepare_ed_GLT_from_fronimo`  | Full preparation pipeline: header + facsimile + sections + finis + system breaks                   |
| `compare_mnums`                | Reports and compares measure counts across all notation types (no file changes)                    |

---

## Other workflows

Additionally to processing via mei-friend using work packages, this repository contains GitHub Actions workflows for operations that run across all files in a caller repository or interact with external services.

### `run_coordinator_multiple_files.yml` — batch processing

Runs the coordinator on all matching MEI files in the caller repository, via [find_files_wrapper.py](scripts/find_files_wrapper.py).

Inputs:

- `workpackage_id` — the work package to execute
- `filetype` — filter by notation type (`dipl`, `ed`, `CMN`, `GLT`, `dipl_CMN`, `dipl_GLT`, `ed_CMN`, `ed_GLT`); all types if omitted
- `exclude` — space-separated list of piece numbers to skip
- `addargs` — additional parameters as a JSON string
- `commit_message` — commit message for the result

### `run_validation.yml` — encoding validation

Runs [validate_encodings.py](scripts/validate_encodings.py) across all files in the caller repository.

### `parse_and_upload_provenance_data.yml` — provenance upload

Generates provenance metadata from the caller repository's MEI files and uploads it to a GraphDB triple store. Requires GraphDB credentials stored as secrets in the caller repository.

### `release_actions.yml` — TU-RDM upload

Uploads all MEI files of a repository to the TU Wien Research Data Management platform. Requires a TU-RDM API token stored as a secret in the caller repository. This workflow is triggered by creating a release of the caller repository.
All scripts needed for this workflow are stored in `scripts/upload_to_RDM/`.

---

## Writing new scripts

Scripts live in [scripts/script_collection.py](scripts/script_collection.py) (or in a new module in the same directory).
Each script function must follow this signature:

```python
def my_script(active_dom: dict, context_doms: list, **params):
    """
    active_dom: {"filename": str, "notationtype": str, "dom": lxml.etree.Element}
    context_doms: list of the same structure, one per sibling .mei file
    params: keyword arguments declared in the work package JSON
    """
    root = active_dom["dom"]

    # ... modify root ...

    active_dom["dom"] = root
    output_message = "Human-readable result shown in the Actions log"
    summary_message = "| table row | for | GitHub summary |"
    return active_dom, output_message, summary_message
```

Rules:

- Raise `RuntimeError` to abort the work package and leave the file unchanged.
- Do not write to disk — the coordinator handles that.
- Look up siblings via `context_doms` by matching `context_dom["notationtype"]`.
- Use `**params` to absorb any parameters from the JSON that the function does not use.

Once the function exists, register it as a work package entry in `work_packages.json` and it becomes available in mei-friend.

[scripts/template_script.py](scripts/template_script.py) provides a minimal starting point.

---

## Connecting a caller repository to this central repository

Create a caller repository from the [caller template](https://github.com/mei-friend/caller-template) — no changes to `caller.yml` are needed.

Then point mei-friend's **"Custom configuration"** field at the raw URL of `work_packages.json` in this repository:

```
https://raw.githubusercontent.com/e-laute/automation/refs/heads/main/scripts/work_packages.json
```

The JSON file already contains the `central_repository`, `branch`, and `automation` fields pointing to this repository.
The E-LAUTE work packages will appear in the mei-friend GitHub Actions panel, ready to use.
