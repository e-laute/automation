"""
Release orchestration script for the upload of MEI files to the TU-RDM platform in the E-LAUTE GitHub Actions workflow.

TODO: add provenance generation
    - auch ins RDM speichern? als Teil vom Source-Datensatz

"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import generate_provenance
import validate_encodings


# TODO: how to proceed with original ILT or FLT files?
# For now, only CMN and GLT allowed because some repos contain old ILT/FLT files.
FILENAME_PATTERN = re.compile(r"^.+_enc_(?:ed|dipl)_(?:CMN|GLT)\.mei$")
WORK_ID_PATTERN = re.compile(r"^(.+_n\d+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run release steps for the upload of MEI files to the TU-RDM platform in the E-LAUTE GitHub Actions workflow."
    )
    parser.add_argument(
        "--caller-repo-path",
        default=os.environ.get("CALLER_REPO_PATH", ""),
        help="Path to caller repo. Defaults to CALLER_REPO_PATH env or <repo_root>/caller-repo.",
    )
    parser.add_argument(
        "--upload-mode",
        choices=["testing", "production"],
        default=os.environ.get("RELEASE_UPLOAD_MODE", "testing"),
        help="Mode used for upload_meis.py.",
    )
    return parser.parse_args()


def resolve_caller_repo_path(
    caller_repo_path_arg: str, repo_root: Path
) -> Path:
    if caller_repo_path_arg:
        return Path(caller_repo_path_arg).resolve()
    return repo_root.joinpath("caller-repo").resolve()


def parse_excluded_ids(caller_repo_path: Path) -> set[str]:
    exclude_file = caller_repo_path.joinpath("EXCLUDE.md")
    if not exclude_file.exists():
        return set()

    excluded: set[str] = set()
    for raw_line in exclude_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        line = re.sub(r"^[-*]\s*\[[ xX]\]\s*", "", line)
        line = re.sub(r"^[-*]\s*", "", line)
        line = line.strip().strip("`")
        line = line.replace(r"\_", "_")
        if not line:
            continue

        candidate = re.split(r"\s+", line, maxsplit=1)[0]
        candidate = candidate.replace(r"\_", "_")
        candidate = candidate.strip("`").strip(",;")
        if candidate:
            excluded.add(candidate)
            excluded.add(get_full_id_from_identifier(candidate))
            excluded.add(get_work_id_from_identifier(candidate))

    return excluded


def get_work_id_from_identifier(identifier: str) -> str:
    """
    Derive a short work_id token from an identifier.
    For filenames, first strip everything after '_enc_' and then apply
    the legacy pattern up to '_n<digits>' (needed for id_table.xlsx keys).
    """
    token = get_full_id_from_identifier(identifier)
    match = WORK_ID_PATTERN.match(token)
    if match:
        return match.group(1)
    return token


def get_full_id_from_identifier(identifier: str) -> str:
    """
    Derive a full identifier token.
    For filenames, this is everything before '_enc_'.
    """
    token = identifier.strip().strip("`").strip(",;")
    if token.endswith(".mei"):
        token = token.removesuffix(".mei")
    if "_enc_" in token:
        return token.split("_enc_", maxsplit=1)[0]
    return token


def get_work_id_from_filename(file_name: str) -> str:
    """
    Derive a work_id from an MEI filename.
    Example: Jud_1523-2_n10_18v_enc_dipl_GLT.mei -> Jud_1523-2_n10
    """
    base_name = file_name.removesuffix(".mei")
    return get_work_id_from_identifier(base_name)


def filename_matches(file_name: str) -> bool:
    return bool(FILENAME_PATTERN.match(file_name))


def discover_eligible_ids(
    caller_repo_path: Path,
    excluded_ids: set[str],
) -> list[str]:
    candidate_ids = sorted(
        folder.name
        for folder in caller_repo_path.iterdir()
        if folder.is_dir() and not folder.name.startswith(".")
    )
    eligible_ids: list[str] = []
    for folder_id in candidate_ids:
        folder_work_id = get_work_id_from_identifier(folder_id)
        if folder_id in excluded_ids or folder_work_id in excluded_ids:
            continue
        eligible_ids.append(folder_id)

    return eligible_ids


def run_validation(caller_repo_path: Path, eligible_ids: Iterable[str]) -> bool:
    print("Validation...")
    failed_id_folders: list[str] = []
    failure_logs: dict[str, str] = {}

    for folder_id in eligible_ids:
        id_folder = caller_repo_path.joinpath(folder_id)
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(
                stdout_buffer
            ), contextlib.redirect_stderr(stderr_buffer):
                folder_valid = validate_encodings.main(str(id_folder))
        except Exception as exc:
            folder_valid = False
            stderr_buffer.write(f"Unhandled validator exception: {exc}\n")

        if not folder_valid:
            failed_id_folders.append(folder_id)
            stdout_value = stdout_buffer.getvalue().strip()
            stderr_value = stderr_buffer.getvalue().strip()
            log_parts: list[str] = []
            if stdout_value:
                log_parts.append("stdout:\n" + stdout_value)
            if stderr_value:
                log_parts.append("stderr:\n" + stderr_value)
            failure_logs[folder_id] = (
                "\n\n".join(log_parts)
                if log_parts
                else "No validator output captured."
            )

    if failed_id_folders:
        print("Validation failed for: " + ", ".join(sorted(failed_id_folders)))
        for folder_id in sorted(failed_id_folders):
            print(f"\n--- Validation log for {folder_id} ---")
            print(failure_logs.get(folder_id, "No validator output captured."))
        return False

    print("Validation OK")
    return True


def run_subprocess(
    command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_derive_on_id_folders(
    repo_root: Path, caller_repo_path: Path, eligible_ids: Iterable[str]
) -> bool:
    print("Derive notation files...")
    derive_script = repo_root.joinpath(
        "scripts", "derive-alternate-tablature-notation-types.py"
    )
    if not derive_script.exists():
        print("Derive step failed: derive script not found.")
        return False

    failed_ids: list[str] = []
    for folder_id in eligible_ids:
        id_folder = caller_repo_path.joinpath(folder_id)
        result = run_subprocess(
            [sys.executable, str(derive_script), str(id_folder)],
            cwd=repo_root,
        )
        if result.returncode != 0:
            failed_ids.append(folder_id)

    if failed_ids:
        print("Derive failed for: " + ", ".join(sorted(failed_ids)))
        return False

    print("Derive OK")
    return True


def run_provenance_on_converted_mei_files(
    caller_repo_path: Path, eligible_ids: Iterable[str], excluded_ids: set[str]
) -> bool:
    print("Generate provenance...")
    failed_files: list[str] = []

    for folder_id in eligible_ids:
        folder_root = caller_repo_path.joinpath(folder_id)
        converted_root = folder_root.joinpath("converted")
        if not converted_root.is_dir():
            continue
        converted_mei_files = sorted(
            path.resolve() for path in converted_root.rglob("*.mei")
        )
        for mei_path in converted_mei_files:
            work_id = get_work_id_from_filename(mei_path.name)
            if work_id in excluded_ids:
                continue
            try:
                generate_provenance.build_provenance_for_mei_file(mei_path)
            except Exception as exc:  # noqa: BLE001
                _ = exc
                failed_files.append(str(mei_path))

    if failed_files:
        print(f"Provenance failed for {len(failed_files)} file(s).")
        return False

    print("Provenance OK")
    return True


def export_generated_files_stub(generated_files: Iterable[str]) -> None:
    _ = generated_files


def stage_converted_mei_files_by_id(
    caller_repo_path: Path,
    eligible_ids: Iterable[str],
    excluded_ids: set[str],
) -> tuple[Path, dict[str, list[str]]]:
    staging_root = Path(tempfile.mkdtemp(prefix="elaute_release_")).resolve()
    files_by_id: dict[str, list[str]] = {}
    for folder_id in eligible_ids:
        folder_root = caller_repo_path.joinpath(folder_id)
        converted_root = folder_root.joinpath("converted")
        if not converted_root.is_dir():
            files_by_id[folder_id] = []
            continue
        converted_sources = sorted(
            path.resolve()
            for path in converted_root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".mei", ".ttl"}
        )
        staged_files: list[str] = []
        for source_path in converted_sources:
            work_id = get_work_id_from_filename(source_path.name)
            if work_id in excluded_ids:
                continue
            relative_path = source_path.relative_to(folder_root)
            target_path = staging_root.joinpath(folder_id, relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            staged_files.append(str(target_path.resolve()))

        files_by_id[folder_id] = staged_files
    return staging_root, files_by_id


def cleanup_converted_directories(
    caller_repo_path: Path, eligible_ids: Iterable[str]
) -> None:
    for folder_id in eligible_ids:
        folder_root = caller_repo_path.joinpath(folder_id)
        converted_dir = folder_root.joinpath("converted")
        if converted_dir.is_dir():
            shutil.rmtree(converted_dir, ignore_errors=True)


def run_upload_on_id_folders(
    repo_root: Path,
    scripts_path: Path,
    workspace_root_for_upload: Path,
    eligible_ids: Iterable[str],
    converted_files_by_id: dict[str, list[str]],
    upload_mode: str,
) -> bool:
    print("Upload...")
    print(f"Upload mode: {upload_mode}")
    failed_ids: list[str] = []
    succeeded_ids: list[str] = []

    base_env = os.environ.copy()
    python_path = base_env.get("PYTHONPATH", "")
    if python_path:
        base_env["PYTHONPATH"] = f"{scripts_path}{os.pathsep}{python_path}"
    else:
        base_env["PYTHONPATH"] = str(scripts_path)

    for folder_id in eligible_ids:
        id_folder = workspace_root_for_upload.joinpath(folder_id)
        selected_files = sorted(set(converted_files_by_id.get(folder_id, [])))
        if not selected_files:
            failed_ids.append(folder_id)
            print(f"{folder_id}: FAILED (no upload files)")
            continue

        manifest_path = ""
        env = base_env.copy()
        env["GITHUB_WORKSPACE"] = str(id_folder)
        env["ELAUTE_ONLY_CONVERTED"] = "1"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                encoding="utf-8",
                delete=False,
            ) as manifest_file:
                manifest_file.write("\n".join(selected_files))
                manifest_path = manifest_file.name
            env["ELAUTE_UPLOAD_FILE_LIST"] = manifest_path
            result = run_subprocess(
                [
                    sys.executable,
                    "-m",
                    "upload_to_RDM.upload_meis",
                    f"--{upload_mode}",
                ],
                cwd=repo_root,
                env=env,
            )
        finally:
            if manifest_path:
                Path(manifest_path).unlink(missing_ok=True)

        # Show only concise per-record outcomes emitted by upload_to_rdm().
        record_lines = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if ": SUCCESS NEW" in line.strip()
            or ": SUCCESS UPDATE" in line.strip()
            or ": FAILED NEW" in line.strip()
            or ": FAILED UPDATE" in line.strip()
        ]
        for line in record_lines:
            print(f"{folder_id} -> {line}")

        if result.returncode != 0:
            failed_ids.append(folder_id)
            expected_token_env = (
                "RDM_TOKEN" if upload_mode == "testing" else "RDM_API_TOKEN_JJ"
            )
            token_present = bool(env.get(expected_token_env, ""))
            print(
                "Upload debug "
                f"[{folder_id}]: mode={upload_mode}, "
                f"returncode={result.returncode}, "
                f"{expected_token_env}={'present' if token_present else 'missing'}"
            )
            print(
                "Upload debug "
                f"[{folder_id}]: command="
                f"{sys.executable} -m upload_to_RDM.upload_meis --{upload_mode}"
            )
            stdout_excerpt = (result.stdout or "").strip()
            stderr_excerpt = (result.stderr or "").strip()
            if stdout_excerpt:
                print(
                    f"Upload debug [{folder_id}] stdout:\n"
                    f"{stdout_excerpt[-4000:]}"
                )
            if stderr_excerpt:
                print(
                    f"Upload debug [{folder_id}] stderr:\n"
                    f"{stderr_excerpt[-4000:]}"
                )
            if not record_lines:
                print(f"{folder_id}: FAILED")
        else:
            succeeded_ids.append(folder_id)
            if not record_lines:
                print(f"{folder_id}: SUCCESS")

    print(
        "Upload summary: "
        f"{len(succeeded_ids)} succeeded, {len(failed_ids)} failed"
    )

    if failed_ids:
        print("Failed folders: " + ", ".join(sorted(failed_ids)))
        return False

    print("Upload OK")
    return True


def main() -> int:
    args = parse_args()
    scripts_path = Path(__file__).resolve().parent
    repo_root = scripts_path.parent

    caller_repo_path = resolve_caller_repo_path(
        args.caller_repo_path, repo_root
    )
    if not caller_repo_path.is_dir():
        print("Release failed: caller repo path not found.")
        return 1

    excluded_ids = parse_excluded_ids(caller_repo_path)
    eligible_ids = discover_eligible_ids(caller_repo_path, excluded_ids)
    if not eligible_ids:
        print("Nothing to process.")
        return 0

    validation_ok = run_validation(caller_repo_path, eligible_ids)
    if not validation_ok:
        print("Release failed at validation.")
        return 1

    derive_ok = run_derive_on_id_folders(
        repo_root, caller_repo_path, eligible_ids
    )
    if not derive_ok:
        print("Release failed at derive step.")
        return 1

    provenance_ok = run_provenance_on_converted_mei_files(
        caller_repo_path, eligible_ids, excluded_ids
    )
    if not provenance_ok:
        print("Release failed at provenance step.")
        return 1

    staging_root, converted_files_by_id = stage_converted_mei_files_by_id(
        caller_repo_path, eligible_ids, excluded_ids
    )
    converted_files = [
        file_path
        for folder_id in eligible_ids
        for file_path in converted_files_by_id.get(folder_id, [])
    ]

    cleanup_converted_directories(caller_repo_path, eligible_ids)

    export_generated_files_stub(converted_files)

    try:
        upload_ok = run_upload_on_id_folders(
            repo_root=repo_root,
            scripts_path=scripts_path,
            workspace_root_for_upload=staging_root,
            eligible_ids=eligible_ids,
            converted_files_by_id=converted_files_by_id,
            upload_mode=args.upload_mode,
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    if not upload_ok:
        print("Release failed at upload step.")
        return 1

    print("Release completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
