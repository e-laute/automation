"""
Upload-script for the E-LAUTE audio files to the TU-RDM platform.

Before running make sure that the IDs in the id_table (Sheets) are well formed
and the audio files in the Drive folder are named so that the full elauteId is present.

Install ffmpeg to enable MP3 conversion (eg. via homebrew: brew install ffmpeg)

Usage:
. .venv/bin/activate
cd scripts/upload_to_RDM
python -m audio_upload

Make sure to set the TESTING_MODE flag to false when ready to upload to the real RDM.

The script will:
1. Copy the audio files from the Google Drive folder to a local folder.
2. Load the form responses from the Google Sheet.
3. Clean the work IDs.
4. Load the ID table.
5. Build the metadata dataframe.
6. Rename the audio files.
7. Extract the audio metadata.
8. Convert the WAV files to MP3.
9. Create the JSON metadata files.
10. Tag the WAV files with the metadata.
11. Upload the new works to the RDM.
12. Update the URL list.
"""

import os
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import taglib
from dotenv import load_dotenv
from wavinfo import WavInfoReader

from rdm_upload_utils import (
    create_related_identifiers,
    get_id_from_api,
    get_records_from_RDM,
    look_up_source_links,
    look_up_source_title,
    make_html_link,
    upload_to_rdm,
)

load_dotenv()
pd.set_option("display.max_columns", None)

ROOT_DIR = Path(__file__).resolve().parent

TESTING_MODE = True
TESTING_WORK_ID = (
    "A-Wn_Mus.Hs._41950_n01, A-Wn_Mus.Hs._41950_n02, A-Wn_Mus.Hs._41950_n03"
)
SELECTED_WORK_IDS = []
COMPARE_IDS_ONLY = False
SKIP_WORK_IDS = []
ONLY_NEW_UPLOADS = True
PROCESS_DELAY_SECONDS = 5

MP3_VBR_QUALITY = 2  # LAME VBR ~190-200 kbps (approx. 192 kbps)

RDM_API_URL = None
RDM_TOKEN = None
ELAUTE_COMMUNITY_ID = None

GOOGLE_DRIVE_DIR = os.getenv("AUDIO_UPLOAD_FORM_DRIVE_DIR_JULIA")
GOOGLE_DRIVE_OWNER = "Julia Jaklin"


def _as_path(path_like):
    return path_like if isinstance(path_like, Path) else Path(path_like)


def _normalize_work_id_alias(work_id):
    if pd.isna(work_id):
        return work_id
    return re.sub(r"^Sotheby_tablature_n(\d+)$", r"Yale_tab_n\1", str(work_id))


def _legacy_sotheby_work_id_alias(work_id):
    if pd.isna(work_id):
        return work_id
    return re.sub(r"^Yale_tab_n(\d+)$", r"Sotheby_tablature_n\1", str(work_id))


def _normalize_work_id_field(work_id_field):
    if pd.isna(work_id_field):
        return work_id_field

    split_ids = [part.strip() for part in str(work_id_field).split(",")]
    normalized_ids = [
        _normalize_work_id_alias(work_id) for work_id in split_ids if work_id
    ]
    return ", ".join(normalized_ids)


def _candidate_work_id_aliases(work_id):
    normalized = _normalize_work_id_alias(work_id)
    legacy = _legacy_sotheby_work_id_alias(normalized)
    return {str(normalized), str(legacy)}


def _normalize_for_matching(text):
    return re.sub(r"\s+", "", str(text or "")).lower()


def _get_requested_work_ids():
    requested_work_ids = []
    if SELECTED_WORK_IDS:
        for work_id in SELECTED_WORK_IDS:
            text = str(work_id).strip()
            if text:
                requested_work_ids.append(text)
    elif TESTING_WORK_ID:
        text = str(TESTING_WORK_ID).strip()
        if text:
            requested_work_ids.append(text)
    return requested_work_ids


def _filter_metadata_for_selected_work_ids(metadata_for_upload):
    requested_work_ids = _get_requested_work_ids()
    if TESTING_MODE and not requested_work_ids:
        raise ValueError(
            "TESTING_MODE requires TESTING_WORK_ID or SELECTED_WORK_IDS."
        )

    if not requested_work_ids:
        return metadata_for_upload, []

    normalized_targets = {
        _normalize_for_matching(_normalize_work_id_alias(work_id))
        for work_id in requested_work_ids
    }
    is_selected = (
        metadata_for_upload["work_id"]
        .astype(str)
        .apply(
            lambda work_id: _normalize_for_matching(work_id)
            in normalized_targets
        )
    )
    selected = metadata_for_upload[is_selected].copy()

    if selected.empty:
        raise ValueError(
            "Requested work IDs not found in metadata_for_upload: "
            f"{requested_work_ids}"
        )

    selected_work_ids = list(
        dict.fromkeys(selected["work_id"].dropna().astype(str).tolist())
    )
    mode_label = "TESTING" if TESTING_MODE else "PRODUCTION"
    print(
        f"{mode_label} mode: limiting upload to "
        f"{len(selected_work_ids)} selected work_ids"
    )
    print(f"Selected work_ids: {selected_work_ids}")
    return selected, selected_work_ids


def _filter_metadata_for_skipped_work_ids(metadata_for_upload):
    if not SKIP_WORK_IDS:
        return metadata_for_upload

    normalized_skip_ids = {
        _normalize_work_id_alias(str(work_id).strip())
        for work_id in SKIP_WORK_IDS
        if str(work_id).strip()
    }
    if not normalized_skip_ids:
        return metadata_for_upload

    skip_ids_for_matching = {
        _normalize_for_matching(work_id) for work_id in normalized_skip_ids
    }
    is_skipped = (
        metadata_for_upload["work_id"]
        .astype(str)
        .apply(
            lambda work_id: _normalize_for_matching(work_id)
            in skip_ids_for_matching
        )
    )
    skipped_rows = metadata_for_upload[is_skipped]
    if not skipped_rows.empty:
        skipped_work_ids = list(
            dict.fromkeys(skipped_rows["work_id"].dropna().astype(str).tolist())
        )
        print("Skipping work_ids from SKIP_WORK_IDS: " f"{skipped_work_ids}")

    return metadata_for_upload[~is_skipped].copy()


def _find_testing_wav_files(release_folder, work_id):
    release_folder = _as_path(release_folder)
    normalized_work_id = _normalize_for_matching(work_id)
    legacy_normalized_work_id = _normalize_for_matching(
        _legacy_sotheby_work_id_alias(work_id)
    )
    search_terms = {normalized_work_id, legacy_normalized_work_id}
    matches = []
    for wav_path in release_folder.glob("*.wav"):
        normalized_name = _normalize_for_matching(wav_path.name)
        if any(search_term in normalized_name for search_term in search_terms):
            matches.append(wav_path)
    return sorted(matches)


def setup_config():
    global RDM_API_URL
    global RDM_TOKEN
    global ELAUTE_COMMUNITY_ID

    if TESTING_MODE:
        print("🧪 Running in TESTING mode")
        RDM_TOKEN = os.getenv("RDM_TEST_API_TOKEN")
        RDM_API_URL = "https://test.researchdata.tuwien.ac.at/api"
        ELAUTE_COMMUNITY_ID = get_id_from_api(
            "https://test.researchdata.tuwien.ac.at/api/communities/e-laute-test"
        )
    else:
        print("🚀 Running in PRODUCTION mode")
        RDM_TOKEN = os.getenv("RDM_API_TOKEN")
        RDM_API_URL = "https://researchdata.tuwien.ac.at/api"
        ELAUTE_COMMUNITY_ID = get_id_from_api(
            "https://researchdata.tuwien.ac.at/api/communities/e-laute"
        )


def remove_string_from_filenames(root_dir, string_to_remove):
    root_path = _as_path(root_dir)
    for file_path in root_path.rglob("*"):
        if file_path.is_file() and string_to_remove in file_path.name:
            new_filename = file_path.name.replace(string_to_remove, "")
            file_path.rename(file_path.with_name(new_filename))


def copy_drive_files():
    audio_files_folder_path = Path(
        f"drive_files/audio_files_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    if not GOOGLE_DRIVE_DIR:
        raise ValueError(
            "Environment variable AUDIO_UPLOAD_FORM_DRIVE_DIR_JULIA is not set."
        )
    google_drive_root = _as_path(GOOGLE_DRIVE_DIR)
    google_drive_audio_folder = (
        google_drive_root
        / "E-LAUTE audio file and metadata upload (File responses)"
    )

    audio_files_folder_path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        google_drive_audio_folder, audio_files_folder_path, dirs_exist_ok=True
    )

    google_sheet_path = (
        google_drive_root
        / "E-LAUTE audio file and metadata upload (Responses).gsheet"
    )
    shutil.copy2(google_sheet_path, audio_files_folder_path)

    folder_name_mapping = {
        "Upload of public release file in .wav format (File responses)": "release",
        "Optional upload of annotated score (screenshot scan) (File responses)": "score",
        "E-LAUTE audio file and metadata upload (Responses).gsheet": "responses.gsheet",
    }

    for old_name, new_name in folder_name_mapping.items():
        old_path = audio_files_folder_path / old_name
        new_path = audio_files_folder_path / new_name

        if old_path.exists():
            old_path.rename(new_path)
        else:
            print(f"Folder not found: {old_path}, probably already renamed.")

    return audio_files_folder_path


def load_form_responses(audio_files_folder_path):
    responses_path = _as_path(audio_files_folder_path) / "responses.gsheet"
    with responses_path.open() as f:
        content = f.read()

    sheet_id = json.loads(content).get("doc_id")

    form_responses_df = pd.read_csv(
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    )

    form_responses_df.rename(
        columns={
            "Timestamp": "timestamp",
            "Email address": "email",
            "Work ID": "work_id_forms",
            "Upload of public release file in .wav format": "audio_link",
            "License agreement": "licence",
            "Name of the performer": "performer",
            "Instrument": "instrument",
            "Number of courses": "courses",
            "Tuning": "tuning",
            "Pitch level in Hertz": "hertz",
            "Date of lute making (approx.)": "making_date",
            "Maker of the lute": "maker",
            "Source used for performance": "source_type",
            "URL of score": "source_link",
            "Performance note": "performance_comment",
            "Optional upload of annotated score (screenshot/scan)": "annot_score_link",
            "Date of recording": "date",
            "Place of recording": "place",
            "Recording equipment": "microphone",
            "Audio Interface": "audio_interface",
            "Digital Audio Workstation (Audio-Schnittprogramm)": "daw",
            "Name of the producer": "producer",
            "Recording notes": "recording_comment",
            "UUID": "uuid",
            "RecordingID": "recording_id",
        },
        inplace=True,
    )

    return form_responses_df


def clean_work_ids(form_responses_df):
    split_ids = form_responses_df["work_id_forms"].str.split()
    form_responses_df = form_responses_df.copy()
    form_responses_df.loc[:, "work_id"] = split_ids.str[-1]
    form_responses_df.loc[:, "work_id_2"] = split_ids.str[-2].fillna("")
    form_responses_df.loc[:, "work_id_3"] = split_ids.str[-3].fillna("")

    grouped_ids = {
        "A-Wn_Mus.Hs._18688_n16": 2,
        "A-Wn_Mus.Hs._41950_n01": 3,
    }
    for leading_id, group_size in grouped_ids.items():
        marker_column = f"work_id_{group_size}"
        if marker_column not in form_responses_df:
            continue

        form_responses_df.loc[
            form_responses_df[marker_column].str.contains(leading_id, na=False),
            "work_id",
        ] = (
            leading_id
            + ", "
            + form_responses_df[f"work_id_{group_size - 1}"]
            + ", "
            + form_responses_df["work_id"]
            if group_size == 3
            else leading_id + ", " + form_responses_df["work_id"]
        )

    form_responses_df.loc[:, "work_id"] = form_responses_df["work_id"].apply(
        _normalize_work_id_field
    )

    return form_responses_df.drop(columns=["work_id_2", "work_id_3"])


def load_id_table():
    def _pick_column(df, choices):
        for col in choices:
            if col in df.columns:
                return col
        return None

    def _normalize_id_table(raw_df):
        work_col = _pick_column(raw_df, ["work_id", "IDs"])
        title_col = _pick_column(raw_df, ["title", "Title"])
        fol_col = _pick_column(raw_df, ["fol_or_p", "fol", "folio_or_page"])

        missing = []
        if work_col is None:
            missing.append("work_id/IDs")
        if title_col is None:
            missing.append("title")
        if fol_col is None:
            missing.append("fol_or_p")
        if missing:
            raise KeyError(
                "ID table is missing required columns: " + ", ".join(missing)
            )

        id_table = pd.DataFrame(
            {
                "work_id": raw_df[work_col]
                .astype("string")
                .str.strip()
                .replace("", pd.NA)
                .apply(_normalize_work_id_alias),
                "title": raw_df[title_col]
                .astype("string")
                .str.strip()
                .replace("", pd.NA),
                "fol_or_p": raw_df[fol_col]
                .astype("string")
                .str.strip()
                .replace("", pd.NA),
            }
        )

        id_table = id_table.dropna(subset=["work_id"])
        return id_table

    id_csv_path = ROOT_DIR / "tables" / "id_table.csv"
    if not id_csv_path.exists():
        raise FileNotFoundError(
            "Could not find id_table.csv. "
            "Generate it via "
            "'python scripts/upload_to_RDM/build_tables_from_dump.py <path_to_dump.sql>'."
        )

    id_csv_df = pd.read_csv(id_csv_path, dtype="string")
    return _normalize_id_table(id_csv_df)


def load_sources_table():
    sources_csv_path = ROOT_DIR / "tables" / "sources_table.csv"
    if not sources_csv_path.exists():
        raise FileNotFoundError(
            "Could not find sources_table.csv. "
            "Expected in scripts/upload_to_RDM/tables/. "
            "Generate it via "
            "'python scripts/upload_to_RDM/build_tables_from_dump.py <path_to_dump.sql>'."
        )

    sources_df = pd.read_csv(sources_csv_path, dtype="string")
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


def build_metadata_df(form_responses_df, id_table):
    metadata_df = form_responses_df.copy()

    def split_name(value):
        if pd.isna(value):
            return pd.Series(["", ""])
        text = str(value).strip()
        if not text:
            return pd.Series(["", ""])
        parts = [part.strip() for part in text.split(",", 1)]
        if len(parts) >= 2:
            return pd.Series(parts)
        return pd.Series([text, ""])

    def extract_before_n(work_id):
        if pd.isna(work_id):
            return pd.NA

        text = str(work_id).strip()
        if not text:
            return pd.NA

        first_work_id = text.split(", ", 1)[0]
        match = re.search(r"(.+?)(?:\s|_n\d+)", first_work_id)
        return match.group(1) if match else first_work_id

    performer_split = metadata_df["performer"].apply(split_name)
    producer_split = metadata_df["producer"].apply(split_name)

    metadata_df.loc[:, "timestamp"] = pd.to_datetime(
        metadata_df["timestamp"], format="%d/%m/%Y %H:%M:%S", errors="coerce"
    )
    making_date = pd.to_numeric(
        metadata_df["making_date"], errors="coerce"
    ).astype("Int64")
    # Drop first so the column is recreated with Int64 dtype: assigning Int64
    # into the existing float64 column would warn about an incompatible-dtype
    # in-place set (deprecated in pandas).
    metadata_df = metadata_df.drop(columns=["making_date"])
    metadata_df.loc[:, "making_date"] = making_date
    metadata_df.loc[:, "performer_lastname"] = performer_split[0]
    metadata_df.loc[:, "performer_firstname"] = performer_split[1]
    metadata_df.loc[:, "licence"] = "CC BY-SA 4.0"
    metadata_df.loc[:, "producer_lastname"] = producer_split[0]
    metadata_df.loc[:, "producer_firstname"] = producer_split[1]
    metadata_df.loc[:, "recording_id"] = (
        metadata_df["work_id"].str.replace(", ", "_")
        + "_"
        + metadata_df["uuid"]
    )
    metadata_df.loc[:, "source_id"] = metadata_df["work_id"].apply(
        extract_before_n
    )

    merged_df = metadata_df.merge(
        id_table[["work_id", "title", "fol_or_p"]], on="work_id", how="left"
    )

    missing_ids_df = merged_df[
        merged_df["title"].isna() | merged_df["fol_or_p"].isna()
    ]
    missing_ids = missing_ids_df["work_id"].tolist()

    for missing_id in missing_ids:
        split_ids = missing_id.split(", ")
        titles = []
        fols = []

        for split_id in split_ids:
            if split_id in id_table["work_id"].tolist():
                title = id_table.loc[
                    id_table["work_id"] == split_id, "title"
                ].values[0]
                fol = id_table.loc[
                    id_table["work_id"] == split_id, "fol_or_p"
                ].values[0]
                titles.append(title)
                fols.append(fol)

        if titles or fols:
            cleaned_titles = [
                str(value).strip()
                for value in titles
                if pd.notna(value) and str(value).strip()
            ]
            cleaned_fols = [
                str(value).strip()
                for value in fols
                if pd.notna(value) and str(value).strip()
            ]

            concatenated_title = (
                ", ".join(cleaned_titles) if cleaned_titles else pd.NA
            )
            concatenated_fol = (
                ", ".join(cleaned_fols) if cleaned_fols else pd.NA
            )
            merged_df.loc[merged_df["work_id"] == missing_id, "title"] = (
                concatenated_title
            )
            merged_df.loc[merged_df["work_id"] == missing_id, "fol_or_p"] = (
                concatenated_fol
            )

    remaining_missing = merged_df[
        merged_df["title"].isna() | merged_df["fol_or_p"].isna()
    ]
    missing_fol_only = merged_df[merged_df["fol_or_p"].isna()]
    if not missing_fol_only.empty:
        missing_fol_work_ids = list(
            dict.fromkeys(missing_fol_only["work_id"].dropna().tolist())
        )
        print(f"fol_or_p is missing for the work_ids: {missing_fol_work_ids}")

    if not remaining_missing.empty:
        print("The following work_ids could not be found in id_table:")
        missing_work_ids = list(
            dict.fromkeys(remaining_missing["work_id"].dropna().tolist())
        )
        print(missing_work_ids)
        print(
            "Skipping upload for these work_ids because required id_table "
            "fields are missing."
        )
        merged_df = merged_df[
            ~merged_df["work_id"].isin(missing_work_ids)
        ].copy()
    return merged_df


def build_metadata_for_upload(merged_df):
    metadata_for_upload = merged_df.drop(
        columns=["email", "audio_link", "annot_score_link", "work_id_forms"]
    )
    return metadata_for_upload


def rename_audio_files(
    metadata_for_upload, audio_files_folder_path, target_audio_files=None
):
    release_folder = _as_path(audio_files_folder_path) / "release"
    all_audio_files = []
    if target_audio_files is not None:
        all_audio_files = [
            _as_path(file_path)
            for file_path in target_audio_files
            if _as_path(file_path).exists()
        ]
    elif release_folder.exists():
        for file_path in release_folder.iterdir():
            if file_path.is_file():
                all_audio_files.append(file_path)

    renamed_files_by_work_id = {}

    for row in metadata_for_upload.itertuples():
        work_id = row.work_id
        fol_or_p = row.fol_or_p
        lastname = row.performer_lastname
        firstname = row.performer_firstname
        work_id_candidates = _candidate_work_id_aliases(work_id)

        matching_file = None

        if matching_file is None:
            for file_path in all_audio_files:
                filename = file_path.name
                if any(
                    candidate in filename for candidate in work_id_candidates
                ):
                    matching_file = file_path
                    break

        if matching_file:
            new_filename = f"{work_id}_{fol_or_p}_{lastname}_{firstname}.wav"
            new_filename = re.sub(r'[<>:"/\\|?*]', "", new_filename)
            new_file_path = matching_file.with_name(new_filename)
            if matching_file != new_file_path:
                matching_file.rename(new_file_path)
            renamed_files_by_work_id[work_id] = new_file_path
        else:
            print(f"No file found for work_id: {work_id}")

    return renamed_files_by_work_id


def extract_audio_metadata(folder_path, target_wav_files=None):
    folder = _as_path(folder_path)
    if target_wav_files is not None:
        wav_files = [
            _as_path(file_path)
            for file_path in target_wav_files
            if _as_path(file_path).exists()
        ]
    else:
        wav_files = list(folder.glob("*.wav"))
    metadata_list = []

    for file_path in wav_files:
        filename = file_path.name

        metadata = {
            "filename": filename,
            "file_path": str(file_path),
            "file_size_bytes": file_path.stat().st_size,
            "audio_format": None,
            "encoding": None,
            "sample_rate": None,
            "sample_rate_khz": None,
            "bits_per_sample": None,
            "channels": None,
            "channel_count": None,
            "channel_description": None,
            "encoding_type": None,
            "duration_seconds": None,
            "total_samples": None,
            "byte_rate": None,
            "block_align": None,
            "technical_spec": None,
            "bext_originator": None,
            "bext_date": None,
            "bext_time": None,
            "bext_time_reference": None,
            "bext_coding_history": None,
            "tags": None,
            "error": None,
        }

        try:
            info = WavInfoReader(str(file_path), bext_encoding="utf-8")
            audio_format = info.fmt

            metadata["audio_format"] = audio_format.audio_format
            metadata["sample_rate"] = audio_format.sample_rate
            metadata["bits_per_sample"] = audio_format.bits_per_sample
            metadata["channels"] = audio_format.channel_count
            metadata["byte_rate"] = audio_format.byte_rate
            metadata["block_align"] = audio_format.block_align

            if audio_format.audio_format == 1:
                metadata["encoding"] = "Linear PCM"
            elif audio_format.audio_format == 3:
                metadata["encoding"] = "IEEE Float"
            else:
                metadata["encoding"] = (
                    f"Format Code {audio_format.audio_format}"
                )

            if audio_format.channel_count == 1:
                metadata["channel_description"] = "mono"
            elif audio_format.channel_count == 2:
                metadata["channel_description"] = "stereo"
            else:
                metadata["channel_description"] = (
                    f"{audio_format.channel_count}-channel"
                )

            if hasattr(info, "data") and info.data:
                metadata["duration_seconds"] = (
                    info.data.byte_count / audio_format.byte_rate
                )
                metadata["total_samples"] = (
                    info.data.byte_count // audio_format.block_align
                )

            metadata["sample_rate_khz"] = audio_format.sample_rate / 1000
            metadata["bits_per_sample"] = audio_format.bits_per_sample
            metadata["channel_count"] = audio_format.channel_count
            metadata["encoding_type"] = (
                "PCM"
                if audio_format.audio_format == 1
                else (
                    "IEEE Float"
                    if audio_format.audio_format == 3
                    else f"Format {audio_format.audio_format}"
                )
            )
            metadata["technical_spec"] = (
                f"{audio_format.sample_rate / 1000}kHz/"
                f"{audio_format.bits_per_sample}-bit/"
                f"{audio_format.channel_count}ch"
            )

            if hasattr(info, "bext") and info.bext:
                metadata["bext_originator"] = info.bext.originator
                metadata["bext_date"] = info.bext.originator_date
                metadata["bext_time"] = info.bext.originator_time
                if hasattr(info.bext, "time_reference"):
                    metadata["bext_time_reference"] = info.bext.time_reference
                if hasattr(info.bext, "coding_history"):
                    metadata["bext_coding_history"] = info.bext.coding_history

            try:
                f = taglib.File(str(file_path))
                if f.tags:
                    tags_str = "; ".join(
                        [f"{k}: {', '.join(v)}" for k, v in f.tags.items()]
                    )
                    metadata["tags"] = tags_str
            except Exception:
                pass

        except Exception as e:
            metadata["error"] = str(e)

        metadata_list.append(metadata)

    return pd.DataFrame(metadata_list)


def convert_wav_to_mp3(folder_path, target_wav_files=None):
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "ffmpeg not found in PATH. Please install ffmpeg to enable MP3 "
            "conversion (192 kbps VBR)."
        )

    folder = _as_path(folder_path)
    if target_wav_files is not None:
        wav_files = [
            _as_path(file_path)
            for file_path in target_wav_files
            if _as_path(file_path).exists()
        ]
    else:
        wav_files = list(folder.glob("*.wav"))
    if not wav_files:
        return

    for wav_path in wav_files:
        mp3_path = wav_path.with_suffix(".mp3")
        if mp3_path.exists():
            if mp3_path.stat().st_mtime >= wav_path.stat().st_mtime:
                continue

        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-q:a",
            str(MP3_VBR_QUALITY),
            str(mp3_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed for {wav_path}: {result.stderr.strip()}"
            )


def create_json_metadata(row, audio_metadata_df):
    def safe_get(value, default=""):
        if pd.isna(value):
            return default
        return value

    work_id = row.get("work_id")
    audio_info = None

    for _, audio_row in audio_metadata_df.iterrows():
        if work_id in audio_row.get("filename", ""):
            audio_info = audio_row
            break

    json_metadata = {
        "work_id": safe_get(row.get("work_id")),
        "source_id": safe_get(row.get("source_id")),
        "folio_page": safe_get(row.get("fol_or_p")),
        "title": f"{safe_get(row.get('title'))}",
        "creator": [
            f"Performer: {safe_get(row.get('performer'))}",
        ],
        "subject": [
            "Lute music",
            "16th century music",
            "Historical performance",
            "E-LAUTE project",
            "Audio recording",
        ],
        "description": (
            f"Audio recording of '{safe_get(row.get('title'))}' "
            f"(Work ID: {safe_get(row.get('work_id'))}) performed on "
            f"{safe_get(row.get('instrument'))} by "
            f"{safe_get(row.get('performer'))}. Part of E-LAUTE project."
        ),
        "publisher": "E-LAUTE Project",
        "contributor": [
            f"Performer: {safe_get(row.get('performer'))}",
        ],
        "date": safe_get(row.get("date")),
        "type": "Sound",
        "format": "audio/wav",
        "identifier": safe_get(row.get("work_id")),
        "source": safe_get(row.get("source_id")),
        "rights": safe_get(row.get("licence"), "CC BY-SA 4.0"),
        "created": safe_get(row.get("date")),
        "modified": datetime.now().isoformat(),
        "isPartOf": "E-LAUTE Digital Edition",
        "location_in_source": (
            f"folio/page {safe_get(row.get('fol_or_p'))} "
            f"in source {safe_get(row.get('source_id'))}"
        ),
        "recording_location": safe_get(row.get("place")),
        "recording_date": safe_get(row.get("date")),
        "microphone": safe_get(row.get("microphone")),
        "audio_interface": safe_get(row.get("audio_interface")),
        "daw": safe_get(row.get("daw")),
        "audio_processing_techniques": [
            "crossfade",
            "fade-in",
            "fade-out",
            "equalization",
            "reverberation",
        ],
        "audio_processing_comment": (
            "Multi-take compilation with crossfade editing, amplitude "
            "normalization, parametric equalization, and algorithmic "
            "reverberation. Final render as uncompressed PCM WAV."
        ),
        "daw_plugins": "Valhalla Room",
        "performer": [
            {
                "firstname": safe_get(row.get("performer_firstname")),
                "lastname": safe_get(row.get("performer_lastname")),
                "fullname": safe_get(row.get("performer")),
            }
        ],
        "producer": (
            [
                {
                    "firstname": safe_get(row.get("producer_firstname")),
                    "lastname": safe_get(row.get("producer_lastname")),
                    "fullname": safe_get(row.get("producer")),
                }
            ]
            if pd.notna(row.get("producer"))
            and row.get("producer") not in [None, ""]
            else []
        ),
        "instrument": safe_get(row.get("instrument")),
        "tuning": safe_get(row.get("tuning")),
        "courses": safe_get(row.get("courses")),
        "pitch_hertz": safe_get(row.get("hertz")),
        "lute_maker": safe_get(row.get("maker")),
        "lute_making_date": safe_get(row.get("making_date")),
        "source_type": safe_get(row.get("source_type")),
        "source_link": (
            safe_get(row.get("source_link"))
            if pd.notna(row.get("source_link"))
            else None
        ),
        "performance_comment": safe_get(row.get("performance_comment")),
        "recording_comment": safe_get(row.get("recording_comment")),
    }

    if row.get("producer") not in [None, ""] and pd.notna(row.get("producer")):
        json_metadata["contributor"].append(
            f"Producer: {safe_get(row.get('producer'))}"
        )
        json_metadata["creator"].append(
            f"Producer: {safe_get(row.get('producer'))}"
        )

    if audio_info is not None:
        json_metadata.update(
            {
                "mo:sample_rate": safe_get(audio_info.get("sample_rate")),
                "mo:bitsPerSample": safe_get(audio_info.get("bits_per_sample")),
                "mo:channels": safe_get(audio_info.get("channel_count")),
                "mo:duration": safe_get(audio_info.get("duration_seconds")),
                "mo:encoding": safe_get(audio_info.get("encoding_type")),
                "audio_filename": safe_get(audio_info.get("filename")),
                "audio_file_size_bytes": safe_get(
                    audio_info.get("file_size_bytes")
                ),
                "audio_channel_description": safe_get(
                    audio_info.get("channel_description")
                ),
                "audio_total_samples": safe_get(
                    audio_info.get("total_samples")
                ),
                "audio_byte_rate": safe_get(audio_info.get("byte_rate")),
                "audio_block_align": safe_get(audio_info.get("block_align")),
            }
        )

    return json_metadata


def create_json_files(df, audio_metadata_df, output_dir):
    output_path = _as_path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    for _index, row in df.iterrows():
        metadata = create_json_metadata(row, audio_metadata_df)
        filename = f"{row['work_id']}_metadata.json"
        with (output_path / filename).open("w") as json_file:
            json.dump(metadata, json_file, indent=4, ensure_ascii=False)


def tag_wav_files_with_metadata(
    folder_path,
    metadata_df,
    sources_table,
    target_wav_files=None,
):
    def safe_str(value):
        if pd.isna(value):
            return ""
        return str(value)

    folder = _as_path(folder_path)
    target_wav_names = None
    if target_wav_files is not None:
        target_wav_names = {
            _as_path(file_path).name for file_path in target_wav_files
        }
    for _index, row in metadata_df.iterrows():
        work_id = row["work_id"]

        matching_files = list(folder.glob(f"{work_id}_*.wav"))
        if target_wav_names is not None:
            matching_files = [
                path for path in matching_files if path.name in target_wav_names
            ]

        if matching_files:
            file_path = matching_files[0]
            filename = file_path.name

            try:
                f = taglib.File(str(file_path))

                f.tags["PERFORMER:LUTE"] = [safe_str(row["performer"])]
                f.tags["TITLE"] = [safe_str(row["title"])]
                f.tags["COMMENT"] = (
                    "This audio file is a recording of the piece "
                    f"\"{row['title']}\", a 16th century lute music piece "
                    "originally notated in lute tablature, created as part "
                    "of the E-LAUTE project (https://e-laute.info). The "
                    "recording preserves and makes historical lute music "
                    "from the German-speaking regions during 1450-1550 "
                    "accessible. The recording is based on the work with "
                    f"the title \"{row['title']}\" and the id "
                    f"\"{row['work_id']}\" in the e-lautedb. It is found "
                    f"on the page(s) or folio(s) {row['fol_or_p']} in the "
                    f"source \"{look_up_source_title(sources_table, row['source_id'])}\" "
                    f"with the source-id \"{row['source_id']}\"."
                )
                f.tags["DATE"] = [safe_str(row["date"])]
                f.tags["GENRE"] = [
                    "Lute Music",
                    "16th Century Music",
                    "Early Music",
                    "Renaissance Music",
                ]
                f.tags["LICENSE"] = [safe_str(row["licence"])]
                f.tags["ELAUTE_WORK_ID"] = [safe_str(row["work_id"])]
                f.tags["ELAUTE_PERFORMER_FIRSTNAME"] = [
                    safe_str(row["performer_firstname"])
                ]
                f.tags["ELAUTE_PERFORMER_LASTNAME"] = [
                    safe_str(row["performer_lastname"])
                ]
                f.tags["ELAUTE_PRODUCER_FIRSTNAME"] = [
                    safe_str(row["producer_firstname"])
                ]
                f.tags["ELAUTE_PRODUCER_LASTNAME"] = [
                    safe_str(row["producer_lastname"])
                ]
                f.tags["ELAUTE_SOURCE_ID"] = [safe_str(row["source_id"])]
                f.tags["ELAUTE_FOL_OR_P"] = [safe_str(row["fol_or_p"])]
                f.tags["ELAUTE_SOURCE_TYPE"] = [safe_str(row["source_type"])]
                f.tags["ELAUTE_INSTRUMENT"] = [safe_str(row["instrument"])]
                f.tags["ELAUTE_LUTE_MAKER"] = [safe_str(row["maker"])]
                f.tags["ELAUTE_COURSES"] = [safe_str(row["courses"])]
                f.tags["ELAUTE_TUNING"] = [safe_str(row["tuning"])]
                f.tags["ELAUTE_HERTZ"] = [safe_str(row["hertz"])]
                f.tags["ELAUTE_LUTE_MAKING_DATE"] = [
                    safe_str(row["making_date"])
                ]
                f.tags["ELAUTE_PERFORMANCE_COMMENT"] = [
                    safe_str(row["performance_comment"])
                ]
                f.tags["ELAUTE_MICROPHONE"] = [safe_str(row["microphone"])]
                f.tags["ELAUTE_RECORDING_COMMENT"] = [
                    safe_str(row["recording_comment"])
                ]
                f.tags["ELAUTE_AUDIO_INTERFACE"] = [
                    safe_str(row["audio_interface"])
                ]
                f.tags["ELAUTE_DAW"] = [safe_str(row["daw"])]
                f.tags["ELAUTE_PRODUCER"] = [safe_str(row["producer"])]
                f.save()

            except Exception as e:
                print(f"Error tagging {filename}: {e}")
        else:
            print(f"No audio file found for work_id: {work_id}")


def create_description(row, sources_table):
    links = look_up_source_links(sources_table, row["source_id"])
    links_stringified = (
        ", ".join(make_html_link(link) for link in links) if links else ""
    )

    source_id = row["source_id"]
    work_number = row["work_id"].split("_")[-1]
    platform_link = make_html_link(
        "https://edition.onb.ac.at/fedora/objects/"
        f"o:lau.{source_id}/methods/sdef:TEI/get?mode={work_number}"
    )

    part1 = (
        f" <h1>Audio recording of a lute piece from the E-LAUTE project</h1>"
        f"<h2>Overview</h2><p>This dataset contains an audio recording of the "
        f'piece "{row["title"]}", a 16th century lute music piece originally '
        "notated in lute tablature, created as part of the E-LAUTE project "
        '(<a href="https://e-laute.info/">https://e-laute.info/</a>). The '
        "recording preserves and makes historical lute music from the "
        "German-speaking regions during 1450-1550 accessible.</p><p>The "
        f'recording is based on the work with the title "{row["title"]}" '
        f'and the id "{row["work_id"]}" in the e-lautedb. It is found on the '
        f'page(s) or folio(s) {row["fol_or_p"]} in the source '
        f'"{look_up_source_title(sources_table, row["source_id"])}" with the '
        f'source-id "{row["source_id"]}".</p>'
    )

    part4 = (
        "<p>The original source and multiple transcriptions of the work can "
        f"be found on the E-LAUTE platform: {platform_link}.</p>"
    )

    part2 = ""
    if links_stringified not in [None, ""]:
        part2 = f"<p>Links to the source: {links_stringified}.</p>"

    part3 = (
        "<h2>Dataset Contents</h2><p>This dataset includes:</p><ul>"
        "<li><strong>Audio file (wav)</strong>: The audio recording of the lute piece "
        "in .wav format</li>  <li><strong>Audio file (mp3)</strong>: The audio recording of the lute piece "
        "in .mp3 format (192 kbps VBR)</li>  <li><strong>Metadata file</strong>: A metadata "
        "file with detailed information about the recording in .json format"
        "</li></ul><h2>About the E-LAUTE Project</h2><p><strong>E-LAUTE: "
        "Electronic Linked Annotated Unified Tablature Edition - The Lute in "
        "the German-Speaking Area 1450-1550</strong></p><p>The E-LAUTE project "
        "creates innovative digital editions of lute tablatures from the "
        "German-speaking area between 1450 and 1550. This interdisciplinary "
        '"open knowledge platform" combines musicology, music practice, music '
        "informatics, and literary studies to transform traditional editions "
        "into collaborative research spaces.</p><p>For more information, visit "
        'the project website: <a href="https://e-laute.info/">'
        "https://e-laute.info/</a></p>"
    )

    return part1 + part4 + part2 + part3


def fill_out_basic_metadata(row, sources_table):
    metadata = {
        "metadata": {
            "title": f'{row["title"]} ({row["work_id"]}) Audio recording',
            "creators": [
                {
                    "person_or_org": {
                        "family_name": row["performer_lastname"],
                        "given_name": row["performer_firstname"],
                        "name": row["performer"],
                        "type": "personal",
                    },
                    "role": {"id": "other", "title": {"en": "Other"}},
                },
            ],
            "contributors": [
                {
                    "person_or_org": {
                        "family_name": row["performer_lastname"],
                        "given_name": row["performer_firstname"],
                        "name": row["performer"],
                        "type": "personal",
                    },
                    "role": {"id": "other", "title": {"en": "Other"}},
                },
                {
                    "affiliations": [{"id": "04d836q62", "name": "TU Wien"}],
                    "person_or_org": {
                        "family_name": "Jaklin",
                        "given_name": "Julia Maria",
                        "name": "Jaklin, Julia Maria",
                        "type": "personal",
                    },
                    "role": {
                        "id": "contactperson",
                        "title": {"en": "Contact person"},
                    },
                },
            ],
            "description": create_description(row, sources_table),
            "publication_date": datetime.today().strftime("%Y-%m-%d"),
            "identifiers": [
                {
                    "identifier": row["work_id"],
                    "scheme": "other",
                }
            ],
            "dates": [
                {
                    "date": row["date"],
                    "description": "Recording date",
                    "type": {"id": "created", "title": {"en": "Created"}},
                }
            ],
            "publisher": "E-LAUTE",
            "references": [{"reference": "https://e-laute.info/"}],
            "related_identifiers": [],
            "resource_type": {
                "id": "sound",
                "title": {"de": "Audio", "en": "Audio"},
            },
            "rights": [
                {
                    "description": {
                        "en": (
                            "Permits almost any use subject to providing credit "
                            "and license notice. Frequently used for media "
                            "assets and educational materials. The most common "
                            "license for Open Access scientific publications. "
                            "Not recommended for software."
                        )
                    },
                    # "icon": "cc-by-sa-icon",
                    "id": "cc-by-sa-4.0",
                    "props": {
                        "scheme": "spdx",
                        "url": (
                            "https://creativecommons.org/licenses/"
                            "by-sa/4.0/legalcode"
                        ),
                    },
                    "title": {
                        "en": (
                            "Creative Commons Attribution Share Alike 4.0 "
                            "International"
                        )
                    },
                }
            ],
            "subjects": [
                {"subject": "16th century"},
                {
                    "id": "http://www.oecd.org/science/inno/38235147.pdf?6.4",
                    "scheme": "FOS",
                    "subject": "Arts (arts, history of arts, performing arts, music)",
                },
                {"subject": "lute music"},
            ],
        }
    }

    if pd.notna(row["producer"]):
        metadata["metadata"]["contributors"].append(
            {
                "person_or_org": {
                    "family_name": row["producer_lastname"],
                    "given_name": row["producer_firstname"],
                    "name": row["producer"],
                    "type": "personal",
                },
                "role": {"id": "producer", "title": {"en": "Producer"}},
            }
        )

    links_to_source = look_up_source_links(sources_table, row["source_id"])
    if links_to_source:
        metadata["metadata"]["related_identifiers"].extend(
            create_related_identifiers(links_to_source)
        )

    return metadata


def get_existing_records_by_work_id():
    records_df = get_records_from_RDM(
        RDM_TOKEN, RDM_API_URL, ELAUTE_COMMUNITY_ID
    )
    if records_df is None or records_df.empty:
        print("No records found in RDM.")
        return {}

    records_df = records_df.dropna(subset=["elaute_id"]).copy()
    if records_df.empty:
        print("No records with E-LAUTE IDs found in RDM.")
        return {}

    records_df.loc[:, "elaute_id"] = records_df["elaute_id"].apply(
        _normalize_work_id_alias
    )
    records_df.loc[:, "updated_dt"] = pd.to_datetime(
        records_df["updated"], errors="coerce"
    )
    records_df = records_df.sort_values("updated_dt")
    latest_records = records_df.groupby("elaute_id").tail(1)
    return dict(zip(latest_records["elaute_id"], latest_records["record_id"]))


def print_rdm_identifier_comparison_and_stop():
    records_df = get_records_from_RDM(
        RDM_TOKEN, RDM_API_URL, ELAUTE_COMMUNITY_ID
    )
    if records_df is None or records_df.empty:
        print("No records returned from RDM.")
        return

    records_df = records_df.dropna(subset=["elaute_id"]).copy()
    if records_df.empty:
        print("No records with identifier scheme 'other' found in RDM.")
        return

    records_df.loc[:, "elaute_id"] = records_df["elaute_id"].apply(
        _normalize_work_id_alias
    )
    records_df.loc[:, "updated_dt"] = pd.to_datetime(
        records_df["updated"], errors="coerce"
    )
    latest_records = (
        records_df.sort_values("updated_dt")
        .groupby("elaute_id")
        .tail(1)
        .sort_values("elaute_id")
    )

    environment = "TEST" if TESTING_MODE else "PRODUCTION"
    print(
        f"\nFetched {len(latest_records)} published work IDs from "
        f"{environment} RDM "
        "(metadata.identifiers with scheme='other'):"
    )
    for _idx, row in latest_records.iterrows():
        print(f"- {row['elaute_id']} -> record {row['record_id']}")

    requested_work_ids = _get_requested_work_ids()
    if requested_work_ids:
        available_ids = set(latest_records["elaute_id"].tolist())
        print("\nRequested work_id comparison against published records:")
        for work_id in requested_work_ids:
            selected = _normalize_work_id_alias(str(work_id))
            is_present = selected in available_ids
            print(
                f"- {work_id} (normalized: {selected}): "
                f"{'FOUND' if is_present else 'NOT FOUND'}"
            )


def process_work_ids(metadata_for_upload, audio_files_path, json_metadata_path):
    existing_records = get_existing_records_by_work_id()
    existing_work_ids = set(existing_records.keys())

    if existing_work_ids:
        print(f"Found {len(existing_work_ids)} existing work_ids in RDM")
    else:
        print("No existing records found in RDM - all records will be created")

    current_work_ids = set(metadata_for_upload["work_id"].tolist())
    print(f"Current batch contains {len(current_work_ids)} work_ids")

    new_work_ids = current_work_ids - existing_work_ids
    existing_work_ids_to_check = current_work_ids & existing_work_ids

    print(f"New work_ids to upload: {len(new_work_ids)}")
    print(
        "Existing work_ids to check for updates: "
        f"{len(existing_work_ids_to_check)}"
    )

    processing_df = metadata_for_upload
    if ONLY_NEW_UPLOADS:
        processing_df = metadata_for_upload[
            metadata_for_upload["work_id"].isin(new_work_ids)
        ].copy()
        print(
            "ONLY_NEW_UPLOADS=True: skipping existing records and only creating "
            f"new uploads ({len(processing_df)} work_ids)."
        )

    audio_dir = _as_path(audio_files_path)
    json_dir = _as_path(json_metadata_path)

    def _collect_upload_files(work_id):
        audio_files = sorted(audio_dir.glob(f"{work_id}_*.wav"))
        mp3_files = sorted(audio_dir.glob(f"{work_id}_*.mp3"))
        json_file = json_dir / f"{work_id}_metadata.json"
        files = []
        if audio_files:
            files.append(audio_files[0])
        if mp3_files:
            files.append(mp3_files[0])
        if json_file.exists():
            files.append(json_file)
        return files

    failed_uploads = []
    rows = list(processing_df.iterrows())
    if not rows:
        print("No work_ids to process after current filters/settings.")
        return new_work_ids, existing_work_ids_to_check
    for index, (_row_idx, row) in enumerate(rows):
        work_id = row["work_id"]
        metadata = fill_out_basic_metadata(row, sources_table)
        files = _collect_upload_files(work_id)
        record_id = existing_records.get(work_id)

        try:
            fails = upload_to_rdm(
                metadata=metadata,
                elaute_id=work_id,
                file_paths=files,
                RDM_API_TOKEN=RDM_TOKEN,
                RDM_API_URL=RDM_API_URL,
                ELAUTE_COMMUNITY_ID=ELAUTE_COMMUNITY_ID,
                record_id=record_id,
            )
            failed_uploads.extend(fails)
        except Exception as e:
            print(f"Error processing work_id {work_id}: {str(e)}")
            failed_uploads.append(work_id)

        if index < len(rows) - 1 and PROCESS_DELAY_SECONDS > 0:
            print(
                "Waiting "
                f"{PROCESS_DELAY_SECONDS} seconds before next work_id..."
            )
            time.sleep(PROCESS_DELAY_SECONDS)

    if failed_uploads:
        print("\nFAILED UPLOADS:")
        for work_id in failed_uploads:
            print(f"   - {work_id}")

    return new_work_ids, existing_work_ids_to_check


def main():
    setup_config()

    if COMPARE_IDS_ONLY:
        print_rdm_identifier_comparison_and_stop()
        print(
            "\nStopped after RDM identifier comparison (COMPARE_IDS_ONLY=True)."
        )
        return

    audio_files_folder_path = copy_drive_files()

    form_responses_df = load_form_responses(audio_files_folder_path)
    form_responses_df = clean_work_ids(form_responses_df)

    id_table = load_id_table()
    global sources_table
    sources_table = load_sources_table()

    merged_df = build_metadata_df(form_responses_df, id_table)
    metadata_for_upload = build_metadata_for_upload(merged_df)
    metadata_for_upload = _filter_metadata_for_skipped_work_ids(
        metadata_for_upload
    )
    release_folder = _as_path(audio_files_folder_path) / "release"
    metadata_for_upload, selected_work_ids = (
        _filter_metadata_for_selected_work_ids(metadata_for_upload)
    )

    rename_targets = None
    if selected_work_ids:
        rename_targets = []
        missing_selection_wavs = []
        for selected_work_id in selected_work_ids:
            matches = _find_testing_wav_files(release_folder, selected_work_id)
            if not matches:
                missing_selection_wavs.append(selected_work_id)
            else:
                rename_targets.extend(matches)
        if missing_selection_wavs:
            raise FileNotFoundError(
                "No WAV file found for selected work_id(s) in release folder: "
                f"{missing_selection_wavs}"
            )

    renamed_files_by_work_id = rename_audio_files(
        metadata_for_upload,
        audio_files_folder_path,
        target_audio_files=rename_targets,
    )

    target_wav_files = None
    if selected_work_ids:
        target_wav_files = []
        unresolved_selected_ids = []
        for selected_work_id in selected_work_ids:
            renamed_wav = renamed_files_by_work_id.get(selected_work_id)
            if renamed_wav is None:
                resolved_wavs = sorted(
                    release_folder.glob(f"{selected_work_id}_*.wav")
                )
                if resolved_wavs:
                    renamed_wav = resolved_wavs[0]
            if renamed_wav is None or not renamed_wav.exists():
                unresolved_selected_ids.append(selected_work_id)
                continue
            target_wav_files.append(renamed_wav)
        if unresolved_selected_ids:
            raise FileNotFoundError(
                "Could not resolve renamed WAV file for selected work_id(s): "
                f"{unresolved_selected_ids}"
            )

    audio_metadata_df = extract_audio_metadata(
        release_folder, target_wav_files=target_wav_files
    )

    tag_wav_files_with_metadata(
        release_folder,
        metadata_for_upload,
        sources_table,
        target_wav_files=target_wav_files,
    )

    convert_wav_to_mp3(release_folder, target_wav_files=target_wav_files)

    audio_files_path = _as_path(audio_files_folder_path) / "release"
    with tempfile.TemporaryDirectory(
        prefix="elaute_json_metadata_"
    ) as temp_json_dir:
        create_json_files(metadata_for_upload, audio_metadata_df, temp_json_dir)
        new_ids, existing_ids = process_work_ids(
            metadata_for_upload, audio_files_path, temp_json_dir
        )

    print("\nProcessing complete:")
    print(f"Total work_ids processed: {len(metadata_for_upload)}")
    print(f"New records created: {len(new_ids)}")
    print(f"Existing records checked: {len(existing_ids)}")


if __name__ == "__main__":
    main()
