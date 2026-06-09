"""
Utility functions for uploading to the TU-RDM platform.
"""

import requests
import os
import json
import hashlib
import argparse
import time
import tempfile
import zipfile
import re
from pathlib import Path
from urllib.parse import quote, urlparse

import pandas as pd

MIN_REQUEST_INTERVAL_SECONDS = 0.85
_LAST_REQUEST_TS = 0.0


def _wait_for_request_slot():
    """
    Enforce a minimum spacing between API requests.
    """
    global _LAST_REQUEST_TS
    if MIN_REQUEST_INTERVAL_SECONDS <= 0:
        _LAST_REQUEST_TS = time.monotonic()
        return

    now = time.monotonic()
    elapsed = now - _LAST_REQUEST_TS
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _LAST_REQUEST_TS = time.monotonic()


def _parse_retry_after_seconds(retry_after_header):
    """
    Parse Retry-After header value as seconds when possible.
    """
    if not retry_after_header:
        return None
    try:
        parsed = int(str(retry_after_header).strip())
        return max(parsed, 0)
    except (TypeError, ValueError):
        return None


def rdm_request(
    method,
    url,
    *,
    allow_retry=False,
    max_attempts=3,
    retry_statuses=(429, 500, 502, 503, 504),
    **kwargs,
):
    """
    Centralized API request wrapper with request pacing.
    Retries are disabled by default to avoid duplicate non-idempotent requests.
    """
    attempts = max_attempts if allow_retry else 1
    attempts = max(1, attempts)

    for attempt in range(1, attempts + 1):
        _wait_for_request_slot()
        response = requests.request(method, url, **kwargs)

        if (
            allow_retry
            and attempt < attempts
            and response.status_code in retry_statuses
        ):
            retry_after = _parse_retry_after_seconds(
                response.headers.get("Retry-After")
            )
            backoff_seconds = retry_after
            if backoff_seconds is None:
                backoff_seconds = min(2 ** (attempt - 1), 8)
            time.sleep(backoff_seconds)
            continue

        return response

    raise RuntimeError(
        f"Request failed after retries: {method} {url}"
    )  # defensive fallback


def get_id_from_api(url):
    """Get community ID from API URL with error handling"""
    try:
        response = rdm_request("GET", url, timeout=30)
        response.raise_for_status()
        return response.json().get("id")
    except requests.exceptions.RequestException:
        return None


def _load_dotenv(env_path):
    """Load key=value pairs from a .env file into os.environ without overriding existing vars."""
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


def setup_for_rdm_api_access(TESTING_MODE=True):

    # TODO: remove need for mapping file and url list
    # fetch that info from RDM

    # see Stackoverflow: https://stackoverflow.com/a/66593457 about use in GitHub Actions
    # variable/secret needs to be passed in the GitHub Action
    # - name: Test env vars for python
    #     run: TEST_SECRET=${{ secrets.MY_TOKEN }} python -c 'import os;print(os.environ['TEST_SECRET'])

    _load_dotenv(Path(__file__).parent / ".env")

    if TESTING_MODE:
        RDM_API_URL = "https://test.researchdata.tuwien.ac.at/api"
        ELAUTE_COMMUNITY_ID = get_id_from_api(
            f"{RDM_API_URL}/communities/e-laute-test"
        )
        print("🧪 Running in GitHubActions TESTING mode")
        RDM_API_TOKEN = os.environ.get("RDM_TEST_API_TOKEN_JJ") or os.environ.get("RDM_TEST_API_TOKEN")
        if not RDM_API_TOKEN:
            raise KeyError("RDM_TEST_API_TOKEN_JJ")
    else:
        RDM_API_URL = "https://researchdata.tuwien.ac.at/api"
        ELAUTE_COMMUNITY_ID = get_id_from_api(
            f"{RDM_API_URL}/communities/e-laute"
        )
        print(" 🚀 Running in GitHubActions PRODUCTION mode")
        RDM_API_TOKEN = os.environ.get("RDM_API_TOKEN_JJ") or os.environ.get("RDM_API_TOKEN")
        if not RDM_API_TOKEN:
            raise KeyError("RDM_API_TOKEN_JJ")

    # Use the repo root when called from GitHub Actions; fallback to home.
    FILES_PATH = os.environ.get("GITHUB_WORKSPACE", os.path.expanduser("~"))

    return (
        RDM_API_URL,
        RDM_API_TOKEN,
        FILES_PATH,
        ELAUTE_COMMUNITY_ID,
    )


def parse_rdm_cli_args(description):
    parser = argparse.ArgumentParser(description=description)
    env_group = parser.add_mutually_exclusive_group()
    env_group.add_argument(
        "--testing",
        action="store_true",
        help="Use the testing RDM instance (default).",
    )
    env_group.add_argument(
        "--production",
        action="store_true",
        help="Use the production RDM instance.",
    )
    args = parser.parse_args()

    testing_mode = not args.production
    if args.testing:
        testing_mode = True

    return testing_mode


def append_unique(items, value):
    if value not in items:
        items.append(value)


def load_selected_upload_files_from_env(error_collector=None):
    """
    Load an explicit upload file list from ELAUTE_UPLOAD_FILE_LIST if provided.
    One absolute file path per line.
    """
    manifest_path = os.environ.get("ELAUTE_UPLOAD_FILE_LIST", "").strip()
    if not manifest_path:
        return None

    if not os.path.exists(manifest_path):
        if error_collector is not None:
            error_collector.append(
                f"Upload manifest not found: {manifest_path}"
            )
        return []

    selected_files = []
    with open(manifest_path, "r", encoding="utf-8") as manifest:
        for raw_line in manifest:
            line = raw_line.strip()
            if not line:
                continue
            file_path = os.path.abspath(line)
            if os.path.isfile(file_path):
                selected_files.append(file_path)

    return list(dict.fromkeys(selected_files))


def get_candidate_upload_files(
    files_path, selected_upload_files=None, error_collector=None
):
    """
    Return the exact list of files to upload.
    Priority:
      1) explicit manifest file list (ELAUTE_UPLOAD_FILE_LIST)
      2) files_path scan, optionally restricted to converted folders.
    Includes MEI and provenance Turtle files.
    """
    if selected_upload_files is not None:
        return selected_upload_files

    selected_from_manifest = load_selected_upload_files_from_env(
        error_collector=error_collector
    )
    if selected_from_manifest is not None:
        return [
            file_path
            for file_path in selected_from_manifest
            if file_path.lower().endswith((".mei", ".ttl"))
        ]

    only_converted = os.environ.get("ELAUTE_ONLY_CONVERTED", "0") == "1"
    files = []
    for root, _dirs, filenames in os.walk(files_path):
        for file in filenames:
            if not file.lower().endswith((".mei", ".ttl")):
                continue
            full_path = os.path.abspath(os.path.join(root, file))
            normalized = full_path.replace("\\", "/")
            if only_converted and "/converted/" not in normalized:
                continue
            files.append(full_path)

    return list(dict.fromkeys(sorted(files)))


def get_candidate_mei_files_from_uploads(candidate_upload_files):
    return [
        file_path
        for file_path in candidate_upload_files
        if file_path.lower().endswith(".mei")
    ]


def get_full_id_from_filename(file_name: str) -> str:
    """
    Return everything before '_enc_' (without extension) if present.
    """
    base_name = os.path.splitext(file_name)[0]
    if "_enc_" in base_name:
        return base_name.split("_enc_", maxsplit=1)[0]
    return base_name


def get_short_work_id_for_lookup(identifier: str) -> str:
    """
    Legacy short work_id used for lookups (e.g. id_table keys): ..._n<digits>.
    Falls back to the full identifier when no '_n<digits>' part exists.
    """
    match = re.match(r"^(.+_n\d+)", identifier)
    if match:
        return match.group(1)
    return identifier


def get_work_id_from_filename_for_lookup(file_name: str) -> str:
    """
    Derive lookup work_id from filename:
    1) cut at '_enc_'
    2) shorten to legacy ..._n<digits> form
    """
    full_id = get_full_id_from_filename(file_name)
    return get_short_work_id_for_lookup(full_id)


def get_work_ids_from_files(candidate_mei_files):
    work_ids = set()
    for file_path in candidate_mei_files:
        file = os.path.basename(file_path)
        work_ids.add(get_work_id_from_filename_for_lookup(file))
    return sorted(list(work_ids))


def get_files_for_work_id(work_id, candidate_mei_files):
    matching_files = []
    for file_path in candidate_mei_files:
        file = os.path.basename(file_path)
        file_work_id = get_work_id_from_filename_for_lookup(file)
        if file_work_id == work_id:
            matching_files.append(file_path)
    return list(dict.fromkeys(sorted(matching_files)))


def get_upload_files_for_work_id(work_id, candidate_upload_files):
    matching_files = []
    for file_path in candidate_upload_files:
        file = os.path.basename(file_path)
        file_work_id = get_work_id_from_filename_for_lookup(file)
        if file_work_id == work_id:
            matching_files.append(file_path)
    return list(dict.fromkeys(sorted(matching_files)))


def prepare_upload_file_paths(work_id, upload_file_paths):
    """
    Bundle all .ttl files into one zip archive.
    """
    ttl_files = sorted(
        p for p in upload_file_paths if p.lower().endswith(".ttl")
    )
    if not ttl_files:
        return list(dict.fromkeys(upload_file_paths)), None

    temp_dir = tempfile.mkdtemp(prefix="elaute_ttl_bundle_")
    safe_work_id = re.sub(r"[^A-Za-z0-9._-]+", "_", work_id)
    zip_path = os.path.join(temp_dir, f"{safe_work_id}_provenance_files.zip")

    with zipfile.ZipFile(
        zip_path, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as zip_file:
        for ttl_path in ttl_files:
            zip_file.write(ttl_path, arcname=os.path.basename(ttl_path))

    with zipfile.ZipFile(zip_path, mode="r") as zip_file:
        zipped_entries = zip_file.namelist()
    if len(zipped_entries) != len(ttl_files):
        raise ValueError(
            f"TTL bundling mismatch for {work_id}: expected {len(ttl_files)} entries, "
            f"got {len(zipped_entries)} in zip."
        )

    prepared_files = [
        p for p in upload_file_paths if not p.lower().endswith(".ttl")
    ]
    prepared_files = list(dict.fromkeys(prepared_files))
    prepared_files.append(zip_path)
    return prepared_files, temp_dir


# Utility: make HTML link
def make_html_link(url):
    return f'<a href="{url}" target="_blank">{url}</a>'


def load_sources_table_csv(
    sources_csv_path="scripts/upload_to_RDM/tables/sources_table.csv",
):
    """
    Load and normalize sources_table.csv generated from the Schoenberg dump.
    """
    csv_path = Path(sources_csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Could not find sources table CSV at {csv_path}. "
            "Generate it via "
            "'python scripts/upload_to_RDM/build_tables_from_dump.py <path_to_dump.sql>'."
        )

    sources_df = pd.read_csv(csv_path, dtype="string")
    required_columns = [
        "ID",
        "Shelfmark",
        "Title",
        "Source_link",
        "RISM_link",
        "VD_16",
    ]
    missing = [col for col in required_columns if col not in sources_df.columns]
    if missing:
        raise KeyError(
            "sources_table.csv is missing required columns: "
            + ", ".join(missing)
        )

    sources_table = pd.DataFrame()
    source_id_series = sources_df["ID"].replace(r"^\s*$", pd.NA, regex=True)
    sources_table["source_id"] = source_id_series.fillna(
        sources_df["Shelfmark"]
    )
    sources_table["Title"] = sources_df["Title"]
    sources_table["Source_link"] = sources_df["Source_link"].fillna("")
    sources_table["RISM_link"] = sources_df["RISM_link"].fillna("")
    sources_table["VD_16"] = sources_df["VD_16"].fillna("")
    return sources_table


# Utility: look up source title in csv created from dump of e-lautedb
def look_up_source_title(sources_table, source_id):
    title_series = sources_table.loc[
        sources_table["source_id"] == source_id, "Title"
    ]
    if not title_series.empty:
        return title_series.values[0]
    return None


# Utility: look up source links (stub, replace with actual lookup if needed)
def look_up_source_links(sources_table, source_id):
    source_link_series = sources_table.loc[
        sources_table["source_id"] == source_id,
        "Source_link",
    ]
    rism_series = sources_table.loc[
        sources_table["source_id"] == source_id,
        "RISM_link",
    ]
    vd16_series = sources_table.loc[
        sources_table["source_id"] == source_id,
        "VD_16",
    ]

    source_link = (
        source_link_series.iloc[0] if not source_link_series.empty else ""
    )
    rism = rism_series.iloc[0] if not rism_series.empty else ""
    vd16 = vd16_series.iloc[0] if not vd16_series.empty else ""

    links = []
    if source_link:
        links.append(source_link)
    if rism:
        links.append(rism)
    if vd16:
        links.append(vd16)

    return links


def create_related_identifiers(links):
    related_identifiers = []
    for link in links:
        normalized_link = _normalize_url_identifier(link)
        if not normalized_link:
            continue
        related_identifiers.append(
            {
                "identifier": normalized_link,
                "relation_type": {
                    "id": "ispartof",
                    "title": {"en": "Is part of"},
                },
                "resource_type": {
                    "id": "other",
                    "title": {"de": "Anderes", "en": "Other"},
                },
                "scheme": "url",
            },
        )
    return related_identifiers


def _normalize_url_identifier(value):
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    candidate = text
    if candidate.startswith("www."):
        candidate = f"https://{candidate}"
    elif "://" not in candidate and " " not in candidate:
        # Handle URLs in the source table that omit scheme.
        head = candidate.split("/", 1)[0]
        if "." in head:
            candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate

    return None


def set_headers(RDM_API_TOKEN):

    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RDM_API_TOKEN}",
    }
    fh = {
        "Accept": "application/json",
        "Content-Type": "application/octet-stream",
        "Authorization": f"Bearer {RDM_API_TOKEN}",
    }
    return h, fh


def get_records_from_RDM(RDM_API_TOKEN, RDM_API_URL, ELAUTE_COMMUNITY_ID):
    """
    Fetch records from the RDM API.
    """
    h, _fh = set_headers(RDM_API_TOKEN)
    records = []
    next_url = f"{RDM_API_URL}/communities/{ELAUTE_COMMUNITY_ID}/records"
    visited_urls = set()

    while next_url:
        if next_url in visited_urls:
            break
        visited_urls.add(next_url)

        response = rdm_request("GET", next_url, headers=h, timeout=60)
        if response.status_code != 200:
            return None

        payload = response.json()
        hits = payload.get("hits", {}).get("hits", [])
        for hit in hits:
            record_id = hit.get("id")
            parent_id = hit.get("parent", {}).get("id")
            file_count = hit.get("files", {}).get("count")
            created = hit.get("created")
            updated = hit.get("updated")
            # Try to extract elaute_id from identifiers (other)
            elaute_id = None
            metadata = hit.get("metadata", {})
            identifiers = metadata.get("identifiers")
            for ident in identifiers or []:
                if ident.get("scheme") == "other":
                    elaute_id = ident.get("identifier")
                    break
            records.append(
                {
                    "elaute_id": elaute_id,
                    "record_id": record_id,
                    "parent_id": parent_id,
                    "file_count": file_count,
                    "created": created,
                    "updated": updated,
                }
            )

        next_url = payload.get("links", {}).get("next")
    return pd.DataFrame(records)


def _extract_metadata_payload(metadata):
    if isinstance(metadata, dict) and "metadata" in metadata:
        return metadata.get("metadata") or {}
    return metadata or {}


def _ensure_elaute_identifier(metadata, elaute_id):
    """
    Ensure metadata contains the E-LAUTE id as an "other" identifier.
    This keeps dedup/update mapping stable across all upload scripts.
    """
    if not isinstance(metadata, dict) or not elaute_id:
        return metadata

    metadata_payload = metadata.setdefault("metadata", {})
    identifiers = metadata_payload.setdefault("identifiers", [])
    if not isinstance(identifiers, list):
        identifiers = []
        metadata_payload["identifiers"] = identifiers

    has_identifier = any(
        isinstance(item, dict)
        and item.get("scheme") == "other"
        and str(item.get("identifier", "")).strip() == str(elaute_id)
        for item in identifiers
    )
    if not has_identifier:
        identifiers.append({"identifier": str(elaute_id), "scheme": "other"})

    return metadata


def _metadata_changed_for_update(
    current_record, new_metadata_payload, fields_to_compare
):
    current_metadata = current_record.get("metadata", {})
    changed_fields = []
    for field in fields_to_compare:
        current_value = current_metadata.get(field)
        new_value = new_metadata_payload.get(field)
        if not compare_hashed_files(current_value, new_value):
            changed_fields.append(field)
    return bool(changed_fields), changed_fields


def _get_remote_file_info(record_id, headers, api_url):
    try:
        response = rdm_request(
            "GET", f"{api_url}/records/{record_id}/files", headers=headers
        )
        if response.status_code != 200:
            return None
        entries = response.json().get("entries", [])
    except Exception:
        return None

    remote_info = {}
    for entry in entries:
        key = entry.get("key")
        if not key:
            continue
        remote_info[key] = {
            "size": entry.get("size"),
            "checksum": entry.get("checksum"),
        }
    return remote_info


def _calculate_local_md5_checksum(file_path):
    digest = hashlib.md5()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            digest.update(chunk)
    return f"md5:{digest.hexdigest()}"


def _files_changed_for_update(record_id, local_file_paths, headers, api_url):
    remote_info = _get_remote_file_info(record_id, headers, api_url)
    if remote_info is None:
        return True

    local_names = {path.name for path in local_file_paths}
    remote_names = set(remote_info.keys())

    if local_names != remote_names:
        return True

    for path in local_file_paths:
        remote_entry = remote_info.get(path.name, {})
        remote_checksum = remote_entry.get("checksum")
        if not remote_checksum:
            return True

        local_checksum = _calculate_local_md5_checksum(path)
        if not compare_hashed_files(remote_checksum, local_checksum):
            return True

    return False


def _upload_initialized_files(
    file_entries, local_file_paths, headers, file_headers
):
    errors = []
    file_links_by_key = {}
    for entry in file_entries:
        key = entry.get("key")
        links = entry.get("links", {})
        if key:
            file_links_by_key[key] = links

    for file_path in local_file_paths:
        filename = file_path.name
        links = file_links_by_key.get(filename, {})
        if "content" not in links or "commit" not in links:
            errors.append(f"Missing upload/commit links for {filename}.")
            continue

        with file_path.open("rb") as fp:
            response = rdm_request(
                "PUT",
                links["content"],
                data=fp,
                headers=file_headers,
            )
        if response.status_code != 200:
            errors.append(
                f"Failed to upload file content {filename} "
                f"(code: {response.status_code}) response: {response.text[:300]}"
            )
            continue

        response = rdm_request("POST", links["commit"], headers=headers)
        if response.status_code != 200:
            errors.append(
                f"Failed to commit file {filename} (code: {response.status_code}) "
                f"response: {response.text[:300]}"
            )

    return errors


def _sync_draft_files_with_local(
    record_id,
    local_file_paths,
    links,
    headers,
    file_headers,
    api_url,
):
    """
    Start from previous-version files, then apply local delta:
    - keep unchanged files
    - delete removed files from draft
    - re-upload only new/changed files
    """
    errors = []

    response = rdm_request(
        "POST",
        f"{api_url}/records/{record_id}/draft/actions/files-import",
        headers=headers,
    )
    if response.status_code not in (200, 201):
        errors.append(
            "Failed to import files from previous version for record "
            f"{record_id} (code: {response.status_code}) "
            f"response: {response.text[:300]}"
        )
        return errors

    response = rdm_request(
        "GET", f"{api_url}/records/{record_id}/draft/files", headers=headers
    )
    if response.status_code != 200:
        errors.append(
            f"Failed to list draft files for record {record_id} "
            f"(code: {response.status_code}) response: {response.text[:300]}"
        )
        return errors
    draft_entries = response.json().get("entries", [])
    draft_files_by_key = {
        entry.get("key"): entry for entry in draft_entries if entry.get("key")
    }

    local_names = {path.name for path in local_file_paths}
    draft_names = set(draft_files_by_key.keys())

    # Remove files that are no longer present locally.
    for filename in sorted(draft_names - local_names):
        encoded_filename = quote(filename, safe="")
        response = rdm_request(
            "DELETE",
            f"{api_url}/records/{record_id}/draft/files/{encoded_filename}",
            headers=headers,
        )
        if response.status_code not in (200, 204):
            errors.append(
                f"Failed to delete draft file {filename} from record {record_id} "
                f"(code: {response.status_code}) response: {response.text[:300]}"
            )

    files_to_upload = []
    for file_path in local_file_paths:
        filename = file_path.name
        entry = draft_files_by_key.get(filename)
        if not entry:
            files_to_upload.append(file_path)
            continue

        remote_checksum = entry.get("checksum")
        local_checksum = _calculate_local_md5_checksum(file_path)
        if not remote_checksum or not compare_hashed_files(
            remote_checksum, local_checksum
        ):
            encoded_filename = quote(filename, safe="")
            response = rdm_request(
                "DELETE",
                f"{api_url}/records/{record_id}/draft/files/{encoded_filename}",
                headers=headers,
            )
            if response.status_code not in (200, 204):
                errors.append(
                    f"Failed to replace changed draft file {filename} "
                    f"(code: {response.status_code}) response: {response.text[:300]}"
                )
                continue
            files_to_upload.append(file_path)

    if files_to_upload:
        file_entries = [{"key": path.name} for path in files_to_upload]
        response = rdm_request(
            "POST",
            links["files"],
            data=json.dumps(file_entries),
            headers=headers,
        )
        if response.status_code != 201:
            errors.append(
                f"Failed to initialize files for record {record_id} "
                f"(code: {response.status_code}) response: {response.text[:300]}"
            )
            return errors
        upload_errors = _upload_initialized_files(
            response.json().get("entries", []),
            files_to_upload,
            headers,
            file_headers,
        )
        errors.extend(upload_errors)

    return errors


def upload_to_rdm(
    metadata,
    elaute_id,
    file_paths,
    RDM_API_TOKEN,
    RDM_API_URL,
    ELAUTE_COMMUNITY_ID,
    record_id=None,
    force_metadata_only_update=False,
):
    metadata = _ensure_elaute_identifier(metadata, elaute_id)
    new_upload = record_id is None
    records_api_url = f"{RDM_API_URL}/records"

    failed_uploads = []
    record_errors = []
    failed_step = None
    local_file_paths = [Path(path) for path in file_paths]
    h, fh = set_headers(RDM_API_TOKEN)
    files_changed = False

    def _mark_failed():
        if elaute_id not in failed_uploads:
            failed_uploads.append(elaute_id)

    def _log_step(message):
        print(f"{elaute_id} [STEP] {message}")

    def _record_error(message, step="unknown"):
        nonlocal failed_step
        if failed_step is None:
            failed_step = step
        record_errors.append(message)
        print(f"{elaute_id} [ERROR] {step}: {message}")

    try:
        if not new_upload:
            _log_step("fetch existing record")
            metadata_payload = _extract_metadata_payload(metadata)
            fields_to_compare = [
                "title",
                "creators",
                "contributors",
                "description",
                "identifiers",
                "dates",
                "publisher",
                "references",
                "related_identifiers",
                "resource_type",
                "rights",
            ]

            r = rdm_request(
                "GET", f"{RDM_API_URL}/records/{record_id}", headers=h
            )
            if r.status_code != 200:
                _record_error(
                    f"Failed to fetch record {record_id} (code: {r.status_code})",
                    step="fetch_record",
                )
                _mark_failed()
                return failed_uploads

            metadata_changed, changed_fields = _metadata_changed_for_update(
                r.json(),
                metadata_payload,
                fields_to_compare,
            )
            _log_step("compare metadata and file checksums")
            if force_metadata_only_update:
                files_changed = False
                _log_step(
                    "force metadata-only update: treat files_changed as False"
                )
            else:
                files_changed = _files_changed_for_update(
                    record_id, local_file_paths, h, RDM_API_URL
                )

            if not metadata_changed and not files_changed:
                _log_step("no changes detected")
                return failed_uploads

            if files_changed:
                _log_step("create new version draft")
                # File changes require a new version draft.
                r = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/versions",
                    headers=h,
                )
                if r.status_code != 201:
                    _record_error(
                        "Failed to create new version for record "
                        f"{record_id} (code: {r.status_code})",
                        step="create_new_version",
                    )
                    _mark_failed()
                    return failed_uploads

                new_record_id = r.json()["id"]
                record_id = new_record_id

                # Explicitly enter draft edit mode for the new version draft.
                _log_step("open draft for version")
                r = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/draft",
                    headers=h,
                )
                if r.status_code not in (200, 201):
                    _record_error(
                        f"Failed to enter edit mode for draft {record_id} "
                        f"(code: {r.status_code})",
                        step="open_draft_for_version",
                    )
                    _mark_failed()
                    return failed_uploads
            else:
                # Metadata-only updates edit the currently published record draft.
                _log_step("open draft for metadata update")
                r = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/draft",
                    headers=h,
                )
                if r.status_code not in (200, 201):
                    _record_error(
                        f"Failed to enter edit mode for draft {record_id} "
                        f"(code: {r.status_code})",
                        step="open_draft_for_metadata_update",
                    )
                    _mark_failed()
                    return failed_uploads

            _log_step("update draft metadata")
            r = rdm_request(
                "PUT",
                f"{RDM_API_URL}/records/{record_id}/draft",
                data=json.dumps(metadata),
                headers=h,
            )
            if r.status_code != 200:
                _record_error(
                    f"Failed to update draft {record_id} (code: {r.status_code}) "
                    f"response: {r.text[:300]}",
                    step="update_draft_metadata",
                )
                _mark_failed()
                return failed_uploads

            # Get links from the draft update response.
            links = r.json()["links"]

            # If file differences were detected, start from previous-version files
            # and only apply local delta.
            if files_changed:
                _log_step("sync draft files")
                sync_errors = _sync_draft_files_with_local(
                    record_id=record_id,
                    local_file_paths=local_file_paths,
                    links=links,
                    headers=h,
                    file_headers=fh,
                    api_url=RDM_API_URL,
                )
                if sync_errors:
                    for err in sync_errors:
                        _record_error(err, step="sync_draft_files")
                    _mark_failed()
                    return failed_uploads

            if not files_changed:
                _log_step("publish updated draft")
                r = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/draft/actions/publish",
                    headers=h,
                )
                if r.status_code not in (200, 201, 202):
                    _record_error(
                        "Failed to publish updated draft for record "
                        f"{record_id} (code: {r.status_code}) "
                        f"response: {r.text[:300]}",
                        step="publish_updated_draft",
                    )
                    _mark_failed()
                    return failed_uploads

            _log_step("update flow complete")
            return failed_uploads

        _log_step("create new record draft")
        # Create new draft record
        r = rdm_request(
            "POST",
            records_api_url,
            data=json.dumps(metadata),
            headers=h,
        )
        if r.status_code != 201:
            _record_error(
                f"Failed to create record (code: {r.status_code}) "
                f"response: {r.text[:300]}",
                step="create_record",
            )
            _mark_failed()
            return failed_uploads

        links = r.json()["links"]
        record_id = r.json()["id"]

        # Keep current per-file initialization behavior for new records.
        _log_step(f"initialize/upload {len(local_file_paths)} file(s)")
        for file_path in local_file_paths:
            filename = file_path.name
            _log_step(f"upload file {filename}")
            r = rdm_request(
                "POST",
                links["files"],
                data=json.dumps([{"key": filename}]),
                headers=h,
            )
            if r.status_code != 201:
                _record_error(
                    f"Failed to initialize file {filename} (code: {r.status_code}) "
                    f"response: {r.text[:300]}",
                    step="init_file_slot",
                )
                _mark_failed()
                return failed_uploads
            upload_errors = _upload_initialized_files(
                r.json().get("entries", []),
                [file_path],
                h,
                fh,
            )
            if upload_errors:
                for err in upload_errors:
                    _record_error(err, step="upload_or_commit_file")
                _mark_failed()
                return failed_uploads

        _log_step("set community review")
        # Set community review request first.
        r = rdm_request(
            "PUT",
            f"{records_api_url}/{record_id}/draft/review",
            headers=h,
            data=json.dumps(
                {
                    "receiver": {"community": ELAUTE_COMMUNITY_ID},
                    "type": "community-submission",
                }
            ),
        )
        if r.status_code != 200:
            _record_error(
                "Failed to set review for record "
                f"{record_id} (code: {r.status_code}) "
                f"response: {r.text[:300]}",
                step="set_community_review",
            )
            _mark_failed()
            return failed_uploads

        _log_step("create curation")
        # Create curation request for the record.
        r = rdm_request(
            "POST",
            f"{RDM_API_URL}/curations",
            headers=h,
            data=json.dumps({"topic": {"record": record_id}}),
        )
        if r.status_code != 201:
            _record_error(
                "Failed to create curation for record "
                f"{record_id} (code: {r.status_code}) "
                f"response: {r.text[:300]}",
                step="create_curation",
            )
            _mark_failed()
            return failed_uploads

        _log_step("submit review")
        # Submit community review request.
        r = rdm_request(
            "POST",
            f"{records_api_url}/{record_id}/draft/actions/submit-review",
            headers=h,
        )
        if r.status_code != 202:
            _record_error(
                "Failed to submit review for record "
                f"{record_id} (code: {r.status_code}) "
                f"response: {r.text[:300]}",
                step="submit_review",
            )
            _mark_failed()
            return failed_uploads

        _log_step("new upload flow complete")
        return failed_uploads
    except Exception as exc:
        _record_error(
            f"Unhandled upload error for {elaute_id}: {exc}",
            step="unexpected_exception",
        )
        _mark_failed()
        return failed_uploads


def compare_hashed_files(current_value, new_value):
    """
    Compare two metadata values by hashing their raw JSON representation.
    Any change (including whitespace, empty fields, ordering before JSON
    normalization, etc.) will count as a difference.
    """

    def _hash_value(value):
        try:
            serialized = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            serialized = str(value)

        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    return _hash_value(current_value) == _hash_value(new_value)


# if __name__ == "__main__":
#     pass
