"""
Coordinates the workpackage with the associated file(s)
"""

import argparse
import importlib
import json
import re
import sys
from pathlib import Path

from lxml import etree

from utils import edit_appInfo, write_to_console, write_to_github_summary

JSON_TYPE_TO_PYTHON_TYPE = {"Number": int, "String": str}


def execute_workpackage(filepath: Path, workpackage: dict, params: dict):
    """
    Parses the file path, loads the specified workpackage from the workpackage file
    and calls the designated scripts on the parsed file.

    :param filepath: the file to be processed
    :param workpackage: the workpackage as a dict
    :param params: parameters required for the workpackage
    :returns: 0 on success, 1 if a script fails
    :rtype: int
    """
    try:
        raw_scripts = workpackage["scripts"]
    except KeyError as e:
        raise KeyError("Faulty work package, missing 'scripts'") from e

    scripts_list = []
    for script_entry in raw_scripts:
        if not isinstance(script_entry, str):
            raise TypeError(
                "Faulty work package, 'scripts' entries must be strings"
            )
        # Accept either one script per list entry or accidental comma-separated entries.
        scripts_list.extend(
            [
                script.strip()
                for script in script_entry.split(",")
                if script.strip()
            ]
        )
    if not scripts_list:
        raise ValueError("Faulty work package, 'scripts' cannot be empty")

    active_dom, tree = parse_and_wrap_dom(filepath)

    # E-LAUTE specific, returns empty list for now
    context_doms = get_context_doms(filepath)

    # scripts in the JSON is a list of module-to-function paths (dir.subdir.module.func)
    # modules_dic contains the path of the module as key (dir.subdir.module) and the loaded module as item
    modules_list = list(
        set([script.rpartition(".")[0] for script in scripts_list])
    )
    try:
        modules_dic = {
            mod: importlib.import_module(mod) for mod in modules_list
        }
    except ImportError as e:
        raise NameError(
            f"Unknown module in scripts list: {', '.join(sorted(modules_list))}"
        ) from e
    output_message_total = ""
    summary_message = ""
    error_message = ""
    for script in scripts_list:
        module_path, _dot, func_name = script.rpartition(".")
        current_func = getattr(modules_dic[module_path], func_name, None)
        if current_func is None:
            raise AttributeError(
                f"Unknown script or wrong module path: {script}"
            )
        # scripts take active_dom:dict, context_dom:list[dict], params:dict
        try:
            script_result = current_func(active_dom, context_doms, **params)
            if isinstance(script_result, tuple) and len(script_result) == 3:
                active_dom, output_message_current, summary_message = (
                    script_result
                )
            else:
                raise ValueError(
                    f"Script {func_name} must return a tuple of length 3"
                )
            output_message_total += (
                f"Script {func_name} was successful"
                f"{', says:\n' + output_message_current if output_message_current else '.'}"
                "\n\n"
            )
        except TypeError as e:
            if "missing" in str(e):
                # Extract argument names inside single quotes
                missing_names = re.findall(r"'(.*?)'", str(e))
                arg_list = ", ".join(missing_names)
                raise KeyError(
                    f"The additional arguments passed are incomplete, {func_name} requires: {arg_list}"
                ) from e
            else:
                # If it's a different kind of TypeError, re-raise it
                raise e
        except RuntimeError as e:
            output_message_total = (
                f"Work package {workpackage['label']} failed. \nSee output of individual scripts or refer to the GitHub link above for further information.\n\n"
                + output_message_total
                + f"Script {func_name} failed, says:\n{e}\n\nNo further scripts executed and no files changed"
            )
            write_to_console(output_message_total)
            return 1

    if workpackage["commitResult"]:
        edit_appInfo(active_dom["dom"], workpackage["label"])
        with open(filepath, "wb") as f:
            tree.write(
                f, encoding="UTF-8", pretty_print=True, xml_declaration=True
            )
    output_message_total = (
        f"Work package {workpackage['label']} was successful. \nSee output of individual scripts or refer to the GitHub link above for further information.\n\n"
        + output_message_total
    )
    write_to_console(output_message_total)
    return 0


def get_context_doms(filepath: Path):
    """
    Return list of dictionaries containing context DOMs from the same directory as the active file.
    E-LAUTE specific but could be adapted for other use cases.

    :param filepath: the file path where to look for context DOMs
    :returns: list of dicts with keys {"filename": str, "dom": etree._Element}
    :rtype: list[dict]
    """

    directory = filepath.parent
    extension = ".mei"
    print(f"Directory of context doms {directory}")
    # 2. Find files with the same extension, excluding the original file, call wrapper
    other_files = [
        parse_and_wrap_dom(f)[0]
        for f in directory.glob(f"*{extension}")
        if f != filepath
    ]

    return other_files


def parse_and_wrap_dom(filepath: Path):
    """
    Parse a file and return a tuple of the wrapped root element dict and the parsed tree.

    :param filepath: The file path of the file to be parsed and wrapped
    :returns: A tuple containing ({"filename": str, "dom": etree._Element}, etree._ElementTree)
    :rtype: tuple[dict, etree._ElementTree]
    """
    tree = etree.parse(filepath, etree.XMLParser(recover=True))
    root = tree.getroot()
    filename = filepath.stem
    notationtype = determine_notationtype(filepath)
    return {
        "filename": filename,
        "dom": root,
        "notationtype": notationtype,
    }, tree


def determine_notationtype(filepath: Path):
    """
    Determines notationtype of mei.
    E-LAUTE specific implementaion: from filename (dipl|ed)_(GLT|FLT|ILT|CMN)

    :param filepath: The filepath from which to compute notation_type
    :type filepath: Path
    """
    # gets end of filename containing notationtype information
    notationtype_re = re.match(
        r".+_enc_((dipl|ed)_(GLT|FLT|ILT|CMN))", filepath.stem
    )
    if notationtype_re is None:
        raise NameError(
            f"{filepath.stem} doesn't fit E_LAUTE naming conventions"
        )
    return notationtype_re.group(1)


def main(workpackage_id: str, filepath: str, addargs: list):
    """
    Parses Arguments, selects file, calls coordinator on files with workpackage
    """
    print("We are in coordinator.main!")
    # TODO misses -nt --notationtype, -e --exclude
    # For now assumes python coordinator.py filepath workpackage additional arguments
    # TODO check for validity of workpackage x filetype, multiple files
    # with open(Path("central-repo", "work_packages.json")) as f:
    #     workpackages_list = json.load(f)
    # workpackage = None
    # for candidate in workpackages_list:
    #     if candidate["id"] == workpackage_id:
    #         workpackage = candidate
    #         break
    try:
        workpackage = json.loads(workpackage_json)
    except json.JSONDecodeError:
        print(f"::error::Invalid JSON for workpackage: {workpackage_json}")
        return 1

    dic_add_args = check_addargs_against_json(
        parse_addargs(parameters), workpackage
    )
    # Hardcode 'caller-repo/' prefix to refer to the caller (source) repository.
    if filepath.startswith("caller-repo"):
        mei_path = Path(filepath)
    else:
        mei_path = Path("caller-repo", filepath)
    print(f"Checking file: {mei_path}")
    if not mei_path.is_file():
        print(f"::error::File not found: '{mei_path}'")
        return 2

    execute_workpackage(mei_path, workpackage, dic_add_args)
    print("::notice::Process completed successfully")
    return 0


def parse_addargs(addargs: str):
    """
    Parse and validate additional arguments from JSON string.

    :param addargs: JSON string with curly braces, or None
    :returns: parsed arguments as a dictionary
    :rtype: dict
    """
    if addargs is None:
        return {}
    try:
        addargs_parsed = json.loads(addargs)
        if not isinstance(addargs_parsed, dict):
            raise TypeError("Parsed JSON is not a dict")
    except Exception as e:
        raise ValueError(
            "Addargs needs to be valid JSON with a top-level object in curly braces (refer to the template)"
        ) from e
    return addargs_parsed


def check_addargs_against_json(addargs_dic: dict, workpackage: dict):
    """
    Check parsed user input against the required parameters in the workpackage JSON.
    Uses defaults if not provided by user.

    :param addargs_dic: parsed user input
    :param workpackage: the chosen workpackage from the JSON
    :returns: dictionary with validated/converted arguments
    :rtype: dict
    """
    params = workpackage["params"]

    return_addargs = {}

    for key, value in params.items():
        if key in addargs_dic:
            if "type" not in value:
                raise KeyError(f"Parameter {key} is missing the type")
            try:
                return_addargs[key] = JSON_TYPE_TO_PYTHON_TYPE[value["type"]](
                    addargs_dic[key]
                )
            except ValueError as e:
                raise ValueError(
                    f"User input for {key} isn't of type {value['type']}"
                ) from e
        elif "default" in value:
            print(
                f"Warning: {key} not in additional arguments, taking default value {key}={value['default']}"
            )
            return_addargs[key] = value["default"]
        else:
            raise ValueError(f"Missing additional argument {key}")

    return return_addargs


def initialize_parser():
    """
    Initialize and return the argument parser.

    :returns: configured ArgumentParser instance
    :rtype: argparse.ArgumentParser
    """
    # TODO misses -nt --notationtype, -e --exclude
    # TODO add parsing of the workpackage JSON file as an argument; for now it is hardcoded to central-repo/work_packages.json
    parser = argparse.ArgumentParser(
        description="Coordinates the execution of scripts in the work package on a file path"
    )

    parser.add_argument("-f", "--filepath", help="A specific filepath")
    parser.add_argument(
        "-id",
        "--workpackage_id",
        required=True,
        help="The id of the work package to be executed, as specified in the work package JSON file. ",
    )
    parser.add_argument(
        "-wp",
        "--workpackage_json",
        help="The work package to be executed, as a json string. ",
    )
    parser.add_argument(
        "-p",
        "--parameters",
        help="Parameters required by the work package, formatted as JSON",
    )
    return parser


if __name__ == "__main__":
    parser = initialize_parser()
    args = parser.parse_args()
    sys.exit(
        main(
            workpackage_id=args.workpackage_id,
            workpackage_json=args.workpackage_json,
            filepath=args.filepath,
            parameters=args.parameters,
        )
    )
