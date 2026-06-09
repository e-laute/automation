"""
Upload-script for the E-LAUTE MEI files to the TU-RDM platform.
Primary usage is via a release of a repository on GitHub, which triggers the workflow that runs this script in production mode.


Usage (testing mode):
cd scripts/upload_to_RDM
python -m upload_meis --testing

Usage (production mode):
cd scripts/upload_to_RDM
python -m upload_meis --production

# TODO: make sure that also the derived files are there and ready to be uploaded to RDM


cd /Users/jaklin/Desktop/LetsEncode/e-laute/automation/scripts
GITHUB_WORKSPACE=upload_to_RDM/test_files/A-Wn_Mus.Hs._18688_n07_8r \
python -m upload_to_RDM.upload_meis --testing


"""

import pandas as pd
import json
import os
import shutil
import sys
import traceback
from lxml import etree

from datetime import datetime
from pathlib import Path


from upload_to_RDM.rdm_upload_utils import (
    append_unique,
    rdm_request,
    get_records_from_RDM,
    set_headers,
    parse_rdm_cli_args,
    setup_for_rdm_api_access,
    get_candidate_upload_files,
    get_candidate_mei_files_from_uploads,
    get_work_ids_from_files,
    get_short_work_id_for_lookup,
    get_files_for_work_id,
    get_upload_files_for_work_id,
    prepare_upload_file_paths,
    load_sources_table_csv,
    look_up_source_links,
    look_up_source_title,
    make_html_link,
    create_related_identifiers,
    compare_hashed_files,
    upload_to_rdm,
)

RDM_API_URL = None
RDM_API_TOKEN = None
FILES_PATH = None
ELAUTE_COMMUNITY_ID = None
SELECTED_UPLOAD_FILES = None
FORCE_METADATA_ONLY_UPDATE = True


errors = []
metadata_df = pd.DataFrame()

sources_table_path = os.environ.get(
    "ELAUTE_SOURCES_TABLE_PATH",
    str(Path(__file__).resolve().parent / "tables" / "sources_table.csv"),
)
sources_table = load_sources_table_csv(sources_table_path)

id_table_path = os.environ.get(
    "ELAUTE_ID_TABLE_PATH",
    str(Path(__file__).resolve().parent / "tables" / "id_table.csv"),
)


def _load_id_table_title_map(id_csv_path):
    csv_path = Path(id_csv_path)
    if not csv_path.exists():
        return {}

    raw_df = pd.read_csv(csv_path, dtype="string")
    work_col = "work_id" if "work_id" in raw_df.columns else "IDs"
    title_col = "title" if "title" in raw_df.columns else "Title"
    if work_col not in raw_df.columns or title_col not in raw_df.columns:
        return {}

    table = raw_df[[work_col, title_col]].copy()
    table.loc[:, work_col] = table[work_col].astype("string").str.strip()
    table.loc[:, title_col] = table[title_col].astype("string").str.strip()
    table = table.dropna(subset=[work_col, title_col])
    table = table[table[title_col] != ""]

    title_map = {}
    for _, row in table.iterrows():
        work_id = str(row[work_col]).strip()
        title = str(row[title_col]).strip()
        if not work_id or not title:
            continue
        short_work_id = get_short_work_id_for_lookup(work_id)
        if short_work_id and short_work_id not in title_map:
            title_map[short_work_id] = title
    return title_map


ID_TABLE_TITLE_BY_WORK_ID = _load_id_table_title_map(id_table_path)


def _emit_work_status(work_id, success, mode, detail=None):
    status = "SUCCESS" if success else "FAILED"
    message = f"{work_id}: {status} {mode}"
    if detail:
        message = f"{message} ({detail})"
    print(message)


def _format_exception_detail(exc):
    exc_type = type(exc).__name__
    exc_text = str(exc).strip()
    if exc_text:
        return f"{exc_type}: {exc_text[:300]}"
    return exc_type


def _as_text(value, default=""):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    return str(value)


def _emit_exception_debug(work_id, mode, exc, context=None):
    print(
        f"{work_id}: DEBUG {mode} exception detail: "
        f"{_format_exception_detail(exc)}"
    )
    if context:
        print(f"{work_id}: DEBUG {mode} context: {context}")
    print(f"{work_id}: DEBUG {mode} traceback:\n{traceback.format_exc()}")


def _resolve_title_from_row(row):
    """
    Resolve a robust, non-empty title from available metadata fields.
    Preserves square brackets and all original characters.
    """
    work_id = _as_text(row.get("work_id")).strip()
    short_work_id = get_short_work_id_for_lookup(work_id) if work_id else ""
    id_table_title = (
        _as_text(ID_TABLE_TITLE_BY_WORK_ID.get(short_work_id)).strip()
        if short_work_id
        else ""
    )

    candidates = [
        id_table_title,
        _as_text(row.get("title")).strip(),
        _as_text(row.get("original_title")).strip(),
        _as_text(row.get("normalized_title")).strip(),
        _as_text(row.get("book_title")).strip(),
    ]

    source_id = _as_text(row.get("source_id")).strip()
    source_title = (
        _as_text(look_up_source_title(sources_table, source_id)).strip()
        if source_id
        else ""
    )
    candidates.append(source_title)

    if work_id:
        candidates.append(work_id)

    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def get_metadata_df_from_mei(mei_file_path):
    try:
        with open(mei_file_path, "rb") as f:
            content = f.read()

        doc = etree.fromstring(content)

        def safe_element_text(element):
            if element is None:
                return ""
            # Collect nested text too (e.g. <title><supplied>...</supplied></title>)
            # so editorial markup does not hide title content.
            text = "".join(element.itertext())
            return text.strip() if text else ""

        # Define namespace for MEI
        ns = {"mei": "http://www.music-encoding.org/ns/mei"}

        # Extract basic metadata. Initialize required keys so lookups
        # never fail later, and preserve arbitrary string content
        # (including square brackets) unchanged.
        metadata = {
            "work_id": "",
            "title": "",
            "source_id": "",
            "fol_or_p": "",
            "shelfmark": "",
            "publication_date": "",
        }

        # Extract work ID from identifier - try multiple locations
        identifier_elem = doc.find(".//mei:identifier", ns)
        if identifier_elem is None:
            # Try alternative locations
            identifier_elem = doc.find(".//mei:work/mei:identifier", ns)
        identifier_text = safe_element_text(identifier_elem)
        if identifier_text:
            metadata["work_id"] = identifier_text
        else:
            errors.append(f"No work ID found in MEI file {mei_file_path}")

        # Extract titles - try multiple locations
        main_title = doc.find('.//mei:title[@type="main"]', ns)
        if main_title is None:
            # Try simple title element
            main_title = doc.find(".//mei:title", ns)
        main_title_text = safe_element_text(main_title)
        if main_title_text:
            metadata["title"] = main_title_text

        # Try work title if main title not found
        if not metadata.get("title"):
            work_title = doc.find(".//mei:work/mei:title", ns)
            work_title_text = safe_element_text(work_title)
            if work_title_text:
                metadata["title"] = work_title_text

        original_title = doc.find('.//mei:title[@type="original"]', ns)
        original_title_text = safe_element_text(original_title)
        if original_title_text:
            metadata["original_title"] = original_title_text

        normalized_title = doc.find('.//mei:title[@type="normalized"]', ns)
        normalized_title_text = safe_element_text(normalized_title)
        if normalized_title_text:
            metadata["normalized_title"] = normalized_title_text

        # Extract publication date
        pub_date = doc.find(".//mei:pubStmt/mei:date[@isodate]", ns)
        if pub_date is not None:
            metadata["publication_date"] = pub_date.get("isodate")

        # Extract folio/page information if not already extracted
        if not metadata.get("fol_or_p"):
            biblscope = doc.find(".//mei:biblScope", ns)
            biblscope_text = safe_element_text(biblscope)
            if biblscope_text:
                metadata["fol_or_p"] = biblscope_text

        # Extract work ID from monograph identifier
        work_id = doc.find(".//mei:analytic/mei:identifier", ns)
        work_id_text = safe_element_text(work_id)
        if work_id_text:
            metadata["work_id"] = work_id_text
            # Extract source_id from work_id by removing everything after the last underscore
            if "_" in work_id_text:
                metadata["source_id"] = work_id_text.rsplit("_", 1)[0]

        # Validate source ID after all extraction attempts
        if not metadata.get("source_id"):
            errors.append(f"No source ID found in MEI file {mei_file_path}")

        shelfmark = doc.find(".//mei:monogr/mei:identifier", ns)
        shelfmark_text = safe_element_text(shelfmark)
        if shelfmark_text:
            metadata["shelfmark"] = shelfmark_text

        book_title = doc.find(".//mei:monogr/mei:title", ns)
        book_title_text = safe_element_text(book_title)
        if book_title_text:
            metadata["book_title"] = book_title_text

        # Final title fallback chain for robustness.
        if not metadata.get("title"):
            metadata["title"] = (
                metadata.get("original_title")
                or metadata.get("normalized_title")
                or metadata.get("book_title")
                or metadata.get("work_id")
                or ""
            )

        # Extract license information
        license_elem = doc.find(".//mei:useRestrict/mei:ref", ns)
        if license_elem is not None:
            metadata["license"] = license_elem.get("target")

        # Extract people and their roles
        people_data = []

        # Get all persName elements with roles
        person_elements = doc.findall(".//mei:persName[@role]", ns)

        for person in person_elements:
            auth_uri = person.get("auth.uri", "")
            # Extract ID from auth.uri (e.g., "https://e-laute.info/data/projectstaff/16" -> "projectstaff-16")
            pers_id = ""
            if auth_uri:
                uri_parts = auth_uri.split("/")
                if len(uri_parts) >= 2:
                    category = uri_parts[-2]  # e.g., "projectstaff"
                    number = uri_parts[-1]  # e.g., "16"
                    pers_id = f"{category}-{number}"
                else:
                    pers_id = auth_uri

            person_info = {
                "file_path": mei_file_path,
                "work_id": metadata.get("work_id", ""),
                "role": person.get("role", ""),
                "auth_uri": auth_uri,
                "pers_id": pers_id,
            }

            # Extract name parts
            forename = person.find("mei:foreName", ns)
            if forename is not None:
                person_info["first_name"] = forename.text.strip()

            famname = person.find("mei:famName", ns)
            if famname is not None:
                person_info["last_name"] = famname.text.strip()

            # Create full name
            full_name_parts = []
            if person_info.get("first_name"):
                full_name_parts.append(person_info["first_name"])
            if person_info.get("last_name"):
                full_name_parts.append(person_info["last_name"])
            person_info["full_name"] = " ".join(full_name_parts)

            people_data.append(person_info)

        # Extract corporate entities (funders, providers, etc.)
        corporate_data = []

        # Get all corpName elements with roles
        corp_elements = doc.findall(".//mei:corpName[@role]", ns)

        for corp in corp_elements:
            # Corporate entities use xml:id instead of auth.uri
            corp_id = corp.get("{http://www.w3.org/XML/1998/namespace}id", "")

            corp_info = {
                "file_path": mei_file_path,
                "work_id": metadata.get("work_id", ""),
                "role": corp.get("role", ""),
                "corp_id": corp_id,
            }

            # Extract organization name - could be in text or in ref/abbr/expan
            ref_elem = corp.find("mei:ref", ns)
            if ref_elem is not None:
                corp_info["url"] = ref_elem.get("target", "")

                # Check for abbreviation and expansion
                abbr_elem = ref_elem.find("mei:abbr", ns)
                if abbr_elem is not None:
                    corp_info["abbreviation"] = abbr_elem.text.strip()

                expan_elem = ref_elem.find("mei:expan", ns)
                if expan_elem is not None:
                    corp_info["full_name"] = expan_elem.text.strip()

                # If no abbr/expan, use the ref text
                if not corp_info.get("abbreviation") and not corp_info.get(
                    "full_name"
                ):
                    corp_info["name"] = (
                        ref_elem.text.strip() if ref_elem.text else ""
                    )
            else:
                # No ref element, use direct text content
                corp_info["name"] = corp.text.strip() if corp.text else ""

            # Use abbreviation as name if available, otherwise use full_name or name
            if not corp_info.get("name"):
                corp_info["name"] = (
                    corp_info.get("abbreviation")
                    or corp_info.get("full_name")
                    or ""
                )

            corporate_data.append(corp_info)

        # Create DataFrames
        people_df = pd.DataFrame(people_data)
        corporate_df = pd.DataFrame(corporate_data)

        # Create a single row metadata DataFrame
        metadata_row = pd.DataFrame([metadata])

        return metadata_row, people_df, corporate_df

    except etree.XMLSyntaxError as e:
        errors.append(f"Error parsing MEI file {mei_file_path}: {str(e)}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    except Exception as e:
        errors.append(f"Error processing MEI file {mei_file_path}: {str(e)}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


def _get_cached_candidate_upload_files():
    global SELECTED_UPLOAD_FILES
    SELECTED_UPLOAD_FILES = get_candidate_upload_files(
        files_path=FILES_PATH,
        selected_upload_files=SELECTED_UPLOAD_FILES,
        error_collector=errors,
    )
    return SELECTED_UPLOAD_FILES


def _get_cached_candidate_mei_files():
    return get_candidate_mei_files_from_uploads(
        _get_cached_candidate_upload_files()
    )


def combine_metadata_for_work_id(work_id, file_paths):
    """
    Extract and combine metadata from all files belonging to a work_id.
    Combines metadata without redundancies but preserves additional people/roles.
    """
    all_metadata = []
    all_people = pd.DataFrame()
    all_corporate = pd.DataFrame()

    for file_path in file_paths:
        metadata_df, people_df, corporate_df = get_metadata_df_from_mei(
            file_path
        )

        if not metadata_df.empty:
            all_metadata.append(metadata_df.iloc[0])

        if not people_df.empty:
            all_people = pd.concat([all_people, people_df], ignore_index=True)

        if not corporate_df.empty:
            all_corporate = pd.concat(
                [all_corporate, corporate_df], ignore_index=True
            )

    if not all_metadata:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # Use the first file's metadata as the base, but merge key fields from other files
    combined_metadata = all_metadata[0].copy()

    # Merge additional metadata fields that might differ between files
    for metadata in all_metadata[1:]:
        # If base metadata is missing a field but another file has it, add it
        for key, value in metadata.items():
            if (
                pd.isna(combined_metadata.get(key))
                or combined_metadata.get(key) == ""
            ):
                if not pd.isna(value) and value != "":
                    combined_metadata[key] = value

    # Special handling for publication_date: choose the latest/most recent date
    publication_dates = []
    for metadata in all_metadata:
        pub_date = metadata.get("publication_date")
        if pub_date and not pd.isna(pub_date) and pub_date != "":
            try:
                # Try to parse the date to ensure it's valid and for comparison
                parsed_date = datetime.strptime(pub_date, "%Y-%m-%d")
                publication_dates.append((pub_date, parsed_date))
            except ValueError:
                # If date parsing fails, skip this date
                continue

    if publication_dates:
        # Sort by parsed date and take the latest one
        latest_date = max(publication_dates, key=lambda x: x[1])
        combined_metadata["publication_date"] = latest_date[0]

    # Remove duplicates from people data - keep unique combinations of person + role
    if not all_people.empty:
        # Normalize names and roles for deduplication
        def normalize_person(row):
            # Lowercase and strip names and roles for robust deduplication
            full_name = str(row.get("full_name", "")).strip().lower()
            role = str(row.get("role", "")).strip().lower()
            return f"{full_name}-{role}"

        all_people["dedup_key"] = all_people.apply(normalize_person, axis=1)
        # Drop duplicates based on normalized key
        all_people = all_people.drop_duplicates(
            subset=["dedup_key"], keep="first"
        )
        all_people = all_people.drop("dedup_key", axis=1)

    # Remove duplicates from corporate data - keep unique combinations of organization + role
    if not all_corporate.empty:

        # Create a composite key for deduplication
        all_corporate["dedup_key"] = all_corporate.apply(
            lambda row: f"{row.get('name', '')}-{row.get('role', '')}", axis=1
        )

        # Keep first occurrence of each unique organization-role combination
        all_corporate = all_corporate.drop_duplicates(
            subset=["dedup_key"], keep="first"
        )
        all_corporate = all_corporate.drop("dedup_key", axis=1)

    return pd.DataFrame([combined_metadata]), all_people, all_corporate


def create_description_for_work(row, file_count, resolved_title=None):
    """
    Create description for a work with multiple files.
    """
    source_id = _as_text(row.get("source_id"))
    work_id = _as_text(row.get("work_id"))
    title = (
        _as_text(resolved_title)
        if resolved_title
        else _resolve_title_from_row(row)
    )
    fol_or_p = _as_text(row.get("fol_or_p"))
    shelfmark = _as_text(row.get("shelfmark"))
    source_title = _as_text(look_up_source_title(sources_table, source_id))

    links = look_up_source_links(sources_table, source_id)
    links_stringified = (
        ", ".join(make_html_link(link) for link in links) if links else ""
    )

    work_number = work_id.split("_")[-1] if work_id else ""
    platform_link = make_html_link(
        f"https://edition.onb.ac.at/fedora/objects/o:lau.{source_id}/methods/sdef:TEI/get?mode={work_number}"
    )

    part1 = f"<h1>Transcriptions in MEI of a lute piece from the E-LAUTE project</h1><h2>Overview</h2><p>This dataset contains transcription files of the piece \"{title}\", a 16th-century lute piece originally notated in lute tablature, created as part of the E-LAUTE project ({make_html_link('https://e-laute.info/')}). The transcriptions preserve and make historical lute music from the German-speaking regions during 1450-1550 accessible.</p><p>They are based on the work with the title \"{title}\" and the id \"{work_id}\" in the e-lautedb. It is found on the page(s) or folio(s) {fol_or_p} in the source \"{source_title}\" with the E-LAUTE source-id \"{source_id}\" and the shelfmark {shelfmark}.</p>"

    part4 = f"<p>Images of the original source and renderings of the transcriptions can be found on the E-LAUTE platform: {platform_link}.</p>"

    if links_stringified not in [None, ""]:
        part2 = f"<p>Links to the source: {links_stringified}.</p>"
    else:
        part2 = ""

    part3 = f'<h2>Dataset Contents</h2><p>This dataset includes {file_count} MEI files (.mei) with different transcription variants (diplomatic/editorial versions in various tablature notations and common music notation) as well as a provenance file in Turtle format (.ttl) for each of the MEI files.</p><p>MEI ({make_html_link("https://music-encoding.org/")}) is an XML-based format for encoding and exchanging structured music notation and related editorial information. Turtle files are a plain-text RDF serialization that represents linked-data statements as triples (subject-predicate-object). Both MEI and Turtle files are text-based and can be opened with any standard text editor.</p><h2>About the E-LAUTE Project</h2><p><strong>E-LAUTE: Electronic Linked Annotated Unified Tablature Edition - The Lute in the German-Speaking Area 1450-1550</strong></p><p>The E-LAUTE project creates innovative digital editions of lute tablatures from the German-speaking area between 1450 and 1550. This interdisciplinary "open knowledge platform" combines musicology, music practice, music informatics, and literary studies to transform traditional editions into collaborative research spaces.</p><p>For more information, visit the project website: {make_html_link('https://e-laute.info/')}</p>'

    return part1 + part4 + part2 + part3


def fill_out_basic_metadata_for_work(
    metadata_row, people_df, corporate_df, file_count
):
    """
    Fill out metadata for RDM upload for a work with multiple files.
    """
    row = metadata_row.iloc[0]
    work_id = _as_text(row.get("work_id"))
    title = _resolve_title_from_row(row)
    publication_date = _as_text(
        row.get("publication_date"), datetime.today().strftime("%Y-%m-%d")
    )

    metadata = {
        "files": {"enabled": True},
        "metadata": {
            "title": f"{title} ({work_id}) MEI Transcriptions",
            "creators": [],
            "contributors": [],
            "description": create_description_for_work(
                row, file_count, resolved_title=title
            ),
            "identifiers": [{"identifier": work_id, "scheme": "other"}],
            "publication_date": datetime.today().strftime("%Y-%m-%d"),
            "dates": [
                {
                    "date": publication_date,
                    "description": "Creation date",
                    "type": {"id": "created", "title": {"en": "Created"}},
                }
            ],
            "publisher": "E-LAUTE",
            "references": [{"reference": "https://e-laute.info/"}],
            "related_identifiers": [],
            "resource_type": {
                "id": "dataset",
                "title": {"de": "Dataset", "en": "Dataset"},
            },
            "rights": [
                {
                    "description": {
                        "en": "Permits almost any use subject to providing credit and license notice. Frequently used for media assets and educational materials. The most common license for Open Access scientific publications. Not recommended for software."
                    },
                    # "icon": "cc-by-sa-icon",
                    "id": "cc-by-sa-4.0",
                    "props": {
                        "scheme": "spdx",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/legalcode",
                    },
                    "title": {
                        "en": "Creative Commons Attribution Share Alike 4.0 International"
                    },
                }
            ],
            "subjects": [
                {
                    "id": "http://www.oecd.org/science/inno/38235147.pdf?6.4",
                    "scheme": "FOS",
                    "subject": "Arts (arts, history of arts, performing arts, music)",
                },
                {"subject": "lute music"},
                {"subject": "MEI"},
                {"subject": "TTL"},
            ],
        },
    }

    # Add people as creators and contributors (same logic as before)
    creator_names = set()
    contributor_names = set()

    # First pass: Add authors as creators
    for _, person in people_df.iterrows():
        if person.get("role") == "author":
            family_name = _as_text(person.get("last_name")) or _as_text(person.get("full_name"))
            if not family_name:
                continue
            person_entry = {
                "person_or_org": {
                    "family_name": family_name,
                    "given_name": person.get("first_name", ""),
                    "name": person.get("full_name", ""),
                    "type": "personal",
                }
            }
            person_entry["role"] = {"id": "other", "title": {"en": "Author"}}
            metadata["metadata"]["creators"].append(person_entry)
            creator_names.add(person.get("full_name", ""))

    # Second pass: Add intabulators as creators
    for _, person in people_df.iterrows():
        if (
            person.get("role") == "intabulator"
            and person.get("full_name", "") not in creator_names
        ):
            family_name = _as_text(person.get("last_name")) or _as_text(person.get("full_name"))
            if not family_name:
                continue
            person_entry = {
                "person_or_org": {
                    "family_name": family_name,
                    "given_name": person.get("first_name", ""),
                    "name": person.get("full_name", ""),
                    "type": "personal",
                }
            }
            person_entry["role"] = {
                "id": "other",
                "title": {"en": "Intabulator"},
            }
            metadata["metadata"]["creators"].append(person_entry)
            creator_names.add(person.get("full_name", ""))

    # Third pass: Add meiEditors and fronimoEditors as creators
    for _, person in people_df.iterrows():
        if (
            person.get("role") in ["meiEditor", "fronimoEditor"]
            and person.get("full_name", "") not in creator_names
        ):
            family_name = _as_text(person.get("last_name")) or _as_text(person.get("full_name"))
            if not family_name:
                continue
            person_entry = {
                "person_or_org": {
                    "family_name": family_name,
                    "given_name": person.get("first_name", ""),
                    "name": person.get("full_name", ""),
                    "type": "personal",
                }
            }
            person_entry["role"] = {"id": "editor", "title": {"en": "Editor"}}
            metadata["metadata"]["creators"].append(person_entry)
            creator_names.add(person.get("full_name", ""))

    # Fourth pass: Add all other roles as contributors (excluding those already added as creators)
    for _, person in people_df.iterrows():
        if person.get("role") not in [
            "author",
            "intabulator",
            "meiEditor",
            "fronimoEditor",
        ]:
            # Create a unique key for this person-role combination
            person_role_key = (
                f"{person.get('full_name', '')}-{person.get('role', '')}"
            )

            if person_role_key not in contributor_names:
                family_name = _as_text(person.get("last_name")) or _as_text(person.get("full_name"))
                if not family_name:
                    continue
                person_entry = {
                    "person_or_org": {
                        "family_name": family_name,
                        "given_name": person.get("first_name", ""),
                        "name": person.get("full_name", ""),
                        "type": "personal",
                    }
                }

                role_mapping = {
                    "metadataContact": {
                        "id": "contactperson",
                        "title": {"en": "Contact person"},
                    },
                    "publisher": {"id": "other", "title": {"en": "Publisher"}},
                }

                person_entry["role"] = role_mapping.get(
                    person.get("role", ""),
                    {"id": "other", "title": {"en": "Other"}},
                )

                metadata["metadata"]["contributors"].append(person_entry)
                contributor_names.add(person_role_key)

    # Add source links as related identifiers
    links_to_source = look_up_source_links(
        sources_table, _as_text(row.get("source_id"))
    )
    if links_to_source:
        metadata["metadata"]["related_identifiers"].extend(
            create_related_identifiers(links_to_source)
        )

    return metadata


def update_records_in_RDM(work_ids_to_update):
    """Update existing records in RDM if metadata has changed."""

    # HTTP Headers
    h, _fh = set_headers(RDM_API_TOKEN)

    # Load existing work_id to record_id mapping
    existing_records = get_records_from_RDM(
        RDM_API_TOKEN, RDM_API_URL, ELAUTE_COMMUNITY_ID
    )
    if existing_records is None or existing_records.empty:
        return [], list(dict.fromkeys(work_ids_to_update))

    # current_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_records = []
    failed_updates = []

    for work_id in work_ids_to_update:
        # Check if work_id exists in mapping
        mapping_row = existing_records[existing_records["elaute_id"] == work_id]
        if mapping_row.empty:
            _emit_work_status(work_id, False, "UPDATE", "missing-record-map")
            continue

        record_id = mapping_row.iloc[0]["record_id"]

        try:
            # Get files for this work_id and combine metadata
            mei_file_paths = get_files_for_work_id(
                work_id, _get_cached_candidate_mei_files()
            )
            if not mei_file_paths:
                if FORCE_METADATA_ONLY_UPDATE:
                    _emit_work_status(
                        work_id,
                        True,
                        "UPDATE",
                        "skipped-missing-input-files",
                    )
                else:
                    append_unique(failed_updates, work_id)
                    _emit_work_status(
                        work_id, False, "UPDATE", "missing-mei-files"
                    )
                continue
            upload_file_paths = []
            if not FORCE_METADATA_ONLY_UPDATE:
                upload_file_paths = get_upload_files_for_work_id(
                    work_id, _get_cached_candidate_upload_files()
                )
                if not upload_file_paths:
                    append_unique(failed_updates, work_id)
                    _emit_work_status(
                        work_id, False, "UPDATE", "missing-upload-files"
                    )
                    continue

            metadata_df, people_df, corporate_df = combine_metadata_for_work_id(
                work_id, mei_file_paths
            )

            if metadata_df.empty:
                append_unique(failed_updates, work_id)
                _emit_work_status(
                    work_id, False, "UPDATE", "metadata-extraction"
                )
                continue

            # Create new metadata structure
            try:
                new_metadata_structure = fill_out_basic_metadata_for_work(
                    metadata_df, people_df, corporate_df, len(mei_file_paths)
                )
            except KeyError as exc:
                append_unique(failed_updates, work_id)
                _emit_work_status(
                    work_id,
                    False,
                    "UPDATE",
                    f"metadata-key-missing: {_format_exception_detail(exc)}",
                )
                _emit_exception_debug(
                    work_id,
                    "UPDATE",
                    exc,
                    context={
                        "metadata_columns": list(metadata_df.columns),
                        "mei_files": mei_file_paths,
                    },
                )
                continue
            new_metadata = new_metadata_structure["metadata"]

            # Fetch current record metadata from RDM
            r = rdm_request(
                "GET", f"{RDM_API_URL}/records/{record_id}", headers=h
            )
            if r.status_code != 200:
                append_unique(failed_updates, work_id)
                _emit_work_status(work_id, False, "UPDATE", "fetch-record")
                continue

            current_record = r.json()
            current_metadata = current_record.get("metadata", {})

            # Compare metadata (excluding auto-generated fields)
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

            # Check for metadata changes
            metadata_changed = False

            for field in fields_to_compare:
                current_value = current_metadata.get(field)
                new_value = new_metadata.get(field)

                if not compare_hashed_files(current_value, new_value):
                    metadata_changed = True

            if not metadata_changed:
                _emit_work_status(work_id, True, "UPDATE", "no-changes")
                continue

            # --- UPDATE ---
            if FORCE_METADATA_ONLY_UPDATE:
                draft_response = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/draft",
                    headers=h,
                )
                if draft_response.status_code not in (200, 201):
                    append_unique(failed_updates, work_id)
                    _emit_work_status(
                        work_id,
                        False,
                        "UPDATE",
                        "open-draft-for-metadata-update",
                    )
                    continue

                update_response = rdm_request(
                    "PUT",
                    f"{RDM_API_URL}/records/{record_id}/draft",
                    data=json.dumps(new_metadata_structure),
                    headers=h,
                )
                if update_response.status_code != 200:
                    append_unique(failed_updates, work_id)
                    _emit_work_status(
                        work_id,
                        False,
                        "UPDATE",
                        "update-draft-metadata",
                    )
                    continue

                publish_response = rdm_request(
                    "POST",
                    f"{RDM_API_URL}/records/{record_id}/draft/actions/publish",
                    headers=h,
                )
                if publish_response.status_code not in (200, 201, 202):
                    append_unique(failed_updates, work_id)
                    _emit_work_status(
                        work_id,
                        False,
                        "UPDATE",
                        "publish-draft-metadata-update",
                    )
                    continue

                append_unique(updated_records, work_id)
                _emit_work_status(work_id, True, "UPDATE", "metadata-only")
            else:
                new_version_data = r.json()
                new_record_id = new_version_data["id"]

                prepared_file_paths, temp_bundle_dir = (
                    prepare_upload_file_paths(work_id, upload_file_paths)
                )
                try:
                    fails = upload_to_rdm(
                        metadata=new_metadata_structure,
                        elaute_id=work_id,
                        file_paths=prepared_file_paths,
                        RDM_API_TOKEN=RDM_API_TOKEN,
                        RDM_API_URL=RDM_API_URL,
                        ELAUTE_COMMUNITY_ID=ELAUTE_COMMUNITY_ID,
                        record_id=new_record_id,
                        force_metadata_only_update=FORCE_METADATA_ONLY_UPDATE,
                    )
                    failed_updates.extend(fails)
                    if not fails:
                        append_unique(updated_records, work_id)
                        _emit_work_status(work_id, True, "UPDATE")
                    else:
                        _emit_work_status(work_id, False, "UPDATE")
                finally:
                    if temp_bundle_dir:
                        shutil.rmtree(temp_bundle_dir, ignore_errors=True)

        except Exception as exc:
            append_unique(failed_updates, work_id)
            _emit_work_status(
                work_id,
                False,
                "UPDATE",
                f"unexpected-error: {_format_exception_detail(exc)}",
            )
            _emit_exception_debug(
                work_id,
                "UPDATE",
                exc,
                context={"record_id": record_id},
            )
            continue

    return updated_records, list(dict.fromkeys(failed_updates))


def process_elaute_ids_for_update_or_create():
    """
    Check which work_ids already exist in RDM and split accordingly.
    Create new records for new work_ids and update existing ones if metadata changed.
    """

    # Get all work_ids from files that currently are to be uploaded (either created or updated)
    work_ids = get_work_ids_from_files(_get_cached_candidate_mei_files())

    if not work_ids:
        return [], []

    existing_records = get_records_from_RDM(
        RDM_API_TOKEN, RDM_API_URL, ELAUTE_COMMUNITY_ID
    )
    if existing_records is None or existing_records.empty:
        existing_work_ids = set()
    else:
        existing_work_ids = set(existing_records["elaute_id"].tolist())

    # Get work_ids from current files
    current_work_ids = set(work_ids)

    # Split into new and existing work_ids
    new_work_ids = current_work_ids - existing_work_ids
    existing_work_ids_to_check = current_work_ids & existing_work_ids

    return list(new_work_ids), list(existing_work_ids_to_check)


def upload_mei_files(work_ids):
    """
    Process and upload MEI files to TU RDM grouped by work_id.
    Each work_id becomes one record with multiple files.
    """
    failed_uploads = []

    if not work_ids:
        return []

    for work_id in work_ids:
        # Get all files for this work_id
        mei_file_paths = get_files_for_work_id(
            work_id, _get_cached_candidate_mei_files()
        )
        upload_file_paths = get_upload_files_for_work_id(
            work_id, _get_cached_candidate_upload_files()
        )

        if not mei_file_paths or not upload_file_paths:
            append_unique(failed_uploads, work_id)
            _emit_work_status(work_id, False, "NEW", "missing-input-files")
            continue

        try:
            # Combine metadata from all files
            metadata_df, people_df, corporate_df = combine_metadata_for_work_id(
                work_id, mei_file_paths
            )

            if metadata_df.empty:
                append_unique(failed_uploads, work_id)
                _emit_work_status(work_id, False, "NEW", "metadata-extraction")
                continue

            # Create RDM metadata
            try:
                metadata = fill_out_basic_metadata_for_work(
                    metadata_df, people_df, corporate_df, len(mei_file_paths)
                )
            except KeyError as exc:
                append_unique(failed_uploads, work_id)
                _emit_work_status(
                    work_id,
                    False,
                    "NEW",
                    f"metadata-key-missing: {_format_exception_detail(exc)}",
                )
                _emit_exception_debug(
                    work_id,
                    "NEW",
                    exc,
                    context={
                        "metadata_columns": list(metadata_df.columns),
                        "mei_files": mei_file_paths,
                    },
                )
                continue

            # ---- UPLOAD ---

            prepared_file_paths, temp_bundle_dir = prepare_upload_file_paths(
                work_id, upload_file_paths
            )
            try:
                fails = upload_to_rdm(
                    metadata=metadata,
                    elaute_id=work_id,
                    file_paths=prepared_file_paths,
                    RDM_API_TOKEN=RDM_API_TOKEN,
                    RDM_API_URL=RDM_API_URL,
                    ELAUTE_COMMUNITY_ID=ELAUTE_COMMUNITY_ID,
                )
                failed_uploads.extend(fails)
                if fails:
                    _emit_work_status(work_id, False, "NEW")
                else:
                    _emit_work_status(work_id, True, "NEW")
            finally:
                if temp_bundle_dir:
                    shutil.rmtree(temp_bundle_dir, ignore_errors=True)

        except Exception as exc:
            append_unique(failed_uploads, work_id)
            _emit_work_status(
                work_id,
                False,
                "NEW",
                f"unexpected-error: {_format_exception_detail(exc)}",
            )
            _emit_exception_debug(
                work_id,
                "NEW",
                exc,
                context={"upload_files": upload_file_paths},
            )
    return list(dict.fromkeys(failed_uploads))


def main() -> int:
    """
    Main function - choose between testing extraction, uploading files, or updating records.
    """

    # TODO: add check for work_ids and RDM_record_ids via RDM_API and check if update or create
    try:
        testing_mode = parse_rdm_cli_args(
            description="Upload MEI files to RDM (testing or production)."
        )

        global RDM_API_URL, RDM_API_TOKEN, FILES_PATH, ELAUTE_COMMUNITY_ID, SELECTED_UPLOAD_FILES
        (
            RDM_API_URL,
            RDM_API_TOKEN,
            FILES_PATH,
            ELAUTE_COMMUNITY_ID,
        ) = setup_for_rdm_api_access(TESTING_MODE=testing_mode)
        SELECTED_UPLOAD_FILES = None
        _get_cached_candidate_upload_files()

        new_work_ids, existing_work_ids = (
            process_elaute_ids_for_update_or_create()
        )
        has_failures = False

        if len(new_work_ids) > 0:
            failed_uploads = upload_mei_files(new_work_ids)
            if failed_uploads:
                has_failures = True

        if len(existing_work_ids) > 0:
            _updated_records, failed_updates = update_records_in_RDM(
                existing_work_ids
            )
            if failed_updates:
                has_failures = True

        return 1 if has_failures else 0
    except Exception as exc:
        print(f"upload_meis.py fatal error: {_format_exception_detail(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
