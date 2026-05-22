import re
import argparse
from pathlib import Path
import coordinator
from utils import write_to_github_summary

FILETYPE_CHOICES = [
    "dipl",
    "ed",
    "CMN",
    "GLT",
    "dipl_CMN",
    "dipl_GLT",
    "ed_CMN",
    "ed_GLT",
]

SUMMARY_HEADER_TABLE = "| Ok? | file_id | dipl_GLT | dipl_CMN | ed_GLT | ed_CMN |\n| --- | --- | --- | --- | --- | --- |\n"

EXCLUDE_ERROR_MESSAGE = "Must all be numbers greater than 0"


def format_filetypes(filetype: str):
    """E-LAUTE specific implementation"""

    if filetype is None:
        return ["dipl_CMN", "ed_CMN", "dipl_GLT", "ed_GLT"]

    match filetype:
        case "dipl":
            return ["dipl_CMN", "ed_CMN"]
        case "ed":
            return ["ed_CMN", "ed_GLT"]
        case "CMN":
            return ["dipl_CMN", "ed_CMN"]
        case "GLT":
            return ["dipl_GLT", "ed_GLT"]
        case _:
            return [filetype]


def format_exclude_files(exclude: list):
    if exclude is None:
        return []
    for id in exclude:
        if not id.isdigit():
            raise ValueError
    return exclude


def get_file_info(filepath: str):
    file_match = re.match(
        r".+n(\d+)_[0-9rv-]+_enc_((ed|dipl)_(CMN|GLT))\.mei", filepath
    )
    if file_match is None:
        return None, None
    return file_match.group(2), file_match.group(1)


def initialize_parser():
    """initializes parser for multiple files"""
    parser = argparse.ArgumentParser(
        description="Coordinates the execution of scripts in the workpackage on filepath"
    )

    parser.add_argument(
        "-e", "--exclude", help="Files to be excluded", nargs="*"
    )
    parser.add_argument(
        "-ft",
        "--filetype",
        help="The filetypes to be included, no filetypes given includes all",
        choices=FILETYPE_CHOICES,
    )
    parser.add_argument(
        "-w",
        "--workpackage_id",
        required=True,
        help="The id of the workpackage to be executed",
    )
    parser.add_argument(
        "-a",
        "--addargs",
        help="Additional arguments required by the workpackage, formatted as json",
    )
    return parser


def root_filter(root: Path):
    if "converted" in str(root):
        return True
    return False


if __name__ == "__main__":
    parser = initialize_parser()
    args = parser.parse_args()
    filetypes = format_filetypes(args.filetype)
    try:
        exclude_files = format_exclude_files(args.exclude)
    except ValueError:
        parser.error(EXCLUDE_ERROR_MESSAGE)

    summary_message = ""
    error_message = ""

    root = Path("caller-repo")
    for root, dirs, filepaths in root.walk():
        dirs.sort()
        if root_filter(root):
            continue
        for filepath in sorted(filepaths):
            filetype, exclude = get_file_info(filepath)
            if filetype is None or exclude is None:
                continue
            if filetype not in filetypes:
                continue
            if exclude in exclude_files:
                continue
            print(f"Wrapper calls coordinator.main with{root / filepath}")
            try:
                summary_message_current, error_message_current = (
                    coordinator.main(
                        workpackage_id=args.workpackage_id,
                        filepath=str((root / filepath)),
                        addargs=args.addargs,
                    )
                )
                summary_message += summary_message_current
                error_message += error_message_current
            except Exception as e:
                print(
                    f"\n{filepath} wasn't processed due to coordinator raising {e}\n"
                )

    write_to_github_summary(
        SUMMARY_HEADER_TABLE + summary_message + "\n\nErrors:\n" + error_message
    )
