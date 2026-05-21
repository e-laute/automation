import copy
import math
import os
import re
import sys
from pathlib import Path

from lxml import etree
from utils import *

ns = {
    "mei": "http://www.music-encoding.org/ns/mei",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def add_sbs_every_n(active_dom: dict, context_doms: list, sbInterval: int, **addargs):
    """
    Adds `<sb>` every n measures

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param n: interval of system beginnings
    :type n: int
    :param addargs: Addional arguments that are unused
    """

    root = active_dom["dom"]

    measures = root.xpath(".//mei:measure", namespaces=ns)

    for count, measure in enumerate(measures):
        if (count + 1) % sbInterval == 0:
            sb = etree.Element("sb")
            parent = measure.getparent()
            parent.insert(parent.index(measure) + 1, sb)

    active_dom["dom"] = root
    output_message = ""
    summary_message = ""

    return active_dom, output_message, summary_message


def remove_all_sbs(active_dom: dict, context_doms: list, **addargs):
    """
    Removes all `<sb>`

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param addargs: Addional arguments that are unused
    """

    root = active_dom["dom"]

    sbs = root.xpath(".//mei:sb", namespaces=ns)

    for sb in sbs:
        parent = sb.getparent()
        parent.remove(sb)

    active_dom["dom"] = root
    output_message = ""
    summary_message = ""

    return active_dom, output_message, summary_message


def add_facs_from_context(
    active_dom: dict, context_doms: list, getElemFrom: str, **addargs
):
    """
    addsbfacs from getPbFroms

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param getElemFrom: string poitning to one notationtype in context_doms
    :type getElemFrom: str
    :param addargs: Addional arguments that are unused
    """
    output_message = ""

    root = active_dom["dom"]
    root: etree.Element = active_dom["dom"]
    for context_dom in context_doms:
        if context_dom["notationtype"] == getElemFrom:
            help_dom = context_dom
            helproot = help_dom["dom"]
            break
    else:
        raise RuntimeError(
            f"add_section_foldir_from_dipl_GLT_to_ed needs context_dom {getElemFrom}, not found"
        )

    oldfacs = root.xpath("//mei:facsimile", namespaces=ns)

    if bool(oldfacs):
        raise RuntimeError(f"{active_dom["filename"]} already has facs")

    facs = helproot.xpath("//mei:facsimile", namespaces=ns)

    if len(facs) == 0:
        raise RuntimeError(f"{help_dom["filename"]} has no facs")

    newfacs = copy.deepcopy(facs[0])
    newfacs.attrib.pop("{http://www.w3.org/XML/1998/namespace}id", None)
    graphics = newfacs.findall(".//mei:graphic", namespaces=ns)
    for graph in graphics:
        graph.attrib.pop("{http://www.w3.org/XML/1998/namespace}id", None)

    music = root.find("./mei:music", namespaces=ns)

    music.insert(0, newfacs)

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message


def _template_function(active_dom: dict, context_doms: list, **addargs):
    """
    template function

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param addargs: Addional arguments that are unused
    """
    output_message = ""

    root = active_dom["dom"]

    xpath_result = root.xpath(".//mei:elem[@attrib='value']", namespaces=ns)

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message


def compare_mnums(active_dom: dict, context_doms: list, **addargs):
    """
    Ouputs number measures and last @n across active and context doms

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param addargs: Addional arguments that are unused
    """

    dipl_GLT = "fnf"
    dipl_CMN = "fnf"
    ed_GLT = "fnf"
    ed_CMN = "fnf"

    doms = [active_dom] + context_doms

    for dom in doms:
        match dom["notationtype"]:
            case "dipl_GLT":
                dipl_GLT = getmnum(dom["dom"])[:2]
            case "dipl_CMN":
                dipl_CMN = getmnum(dom["dom"])[:2]
            case "ed_GLT":
                ed_GLT = getmnum(dom["dom"])
            case "ed_CMN":
                ed_CMN = getmnum(dom["dom"])

    id_match = re.match(
        r".+(n\d+)_([0-9rv-]+)_enc_((ed|dipl)_(CMN|GLT))", active_dom["filename"]
    )
    if id_match:
        id_name = id_match.group(1)
    else:
        id_name = active_dom["filename"]
    mnums_align = (
        dipl_GLT[0] == dipl_CMN[0] == ed_GLT[0] == ed_CMN[0]  # last @n must be the same
        and dipl_GLT[1] == dipl_CMN[1]  # dipland ed should have same number of measures
        and ed_GLT[1] == ed_CMN[1]
        and ed_GLT[2] == ed_CMN[2]  # ed should have same number of corrected measure
    )
    correct = "✅ " if mnums_align else "❌ "
    output_list = [correct, id_name, dipl_GLT, dipl_CMN, ed_GLT, ed_CMN]
    explainer = f"""The table shows all notationtypes found in the directory of {id_name} or fnf for (file not found)
The individual cells show the @n of the last measure, the number of measure elements and a hereustic for measure number.
"""

    output_content = "\t".join(
        [
            "|".join(s.rjust(3) for s in f) if isinstance(f, tuple) else f
            for f in output_list
        ]
    )

    message_content = " | ".join(
        [
            "; ".join(s.rjust(3) for s in f) if isinstance(f, tuple) else f
            for f in output_list
        ]
    )

    output_message = (
        explainer + "File\tdi_GLT\tdi_CMN\ted_GLT\ted_CMN\n" + output_content
    )

    summary_message = "| " + message_content + " |\n"

    return active_dom, output_message, summary_message


def getmnum(root: etree.Element):
    """returns @n of last measure"""

    measures = root.xpath("//mei:measure", namespaces=ns)
    endings = root.xpath("//mei:ending", namespaces=ns)
    has_pickup = measures[0].get("type", "no") == "pickup"

    return (
        measures[-1].get("n", "no_n"),
        str(len(measures)),
        str(len(measures) - int(len(endings) / 2) - has_pickup),
    )


def add_header_from_context(
    active_dom: dict, context_doms: list, projectstaff: str, getElemFrom: str, **addargs
):
    """
    Adds header from dipl_GLT to ed_GLT, dipl_CMN or ed_CMN

    :param active_dom: dict containing {filename:str, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :param getElemFrom: string poitning to one notationtype in context_doms
    :type getElemFrom: str
    :type context_doms: list
    :param addargs: Addional arguments that are unused
    """
    # TODO needs to be adjusted

    output_message = ""

    root = active_dom["dom"]
    root: etree.Element = active_dom["dom"]
    for context_dom in context_doms:
        if context_dom["notationtype"] == getElemFrom:
            help_dom = context_dom
            helproot = help_dom["dom"]
            break
    else:
        raise RuntimeError(
            f"add_section_foldir_from_dipl_GLT_to_ed needs context_dom {getElemFrom}, not found"
        )

    if root.xpath(
        ".//mei:corpName//mei:expan[text()='Electronic Linked Annotated Unified Tablature Edition']",
        namespaces=ns,
    ):
        raise RuntimeError(f"{active_dom["filename"]} already has E-Laute header")

    if not helproot.xpath(
        ".//mei:corpName//mei:expan[text()='Electronic Linked Annotated Unified Tablature Edition']",
        namespaces=ns,
    ):
        raise RuntimeError(f"{active_dom["filename"]} has no header")

    appInfo: etree.Element = root.find(".//mei:appInfo", namespaces=ns)

    help_header: etree.Element = copy.deepcopy(
        helproot.find(".//mei:meiHead", namespaces=ns)
    )

    abbr = help_header.find(".//mei:titlePart/mei:abbr", namespaces=ns)
    if "ed" in active_dom["notationtype"] and "dipl" in help_dom["notationtype"]:
        abbr.getparent().text = "edition in "
    elif "CMN" in active_dom["notationtype"] and "GLT" in help_dom["notationtype"]:
        abbr.clear()
        abbr.set("expan", "Common Music Notation")
        abbr.text = "CMN"
    elif "dipl" in active_dom["notationtype"] and "ed" in help_dom["notationtype"]:
        abbr.getparent().text = "transcription in "
    elif "GLT" in active_dom["notationtype"] and "CMN" in help_dom["notationtype"]:
        abbr.clear()
        abbr.set("expan", "German Lute Tablature")
        abbr.text = "GLT"

    edition = help_header.find(".//mei:edition", namespaces=ns)
    edition.set("resp", f"#{projectstaff}")
    edition.text = f"First {'diplomatic transcription' if 'dipl' in active_dom["notationtype"] else 'edition'} in {'GLT.' if 'GLT' in active_dom["notationtype"] else 'CMN. Lute tuned in A.'}"

    appinfoold = help_header.find(".//mei:appInfo", namespaces=ns)
    encodingDesc = appinfoold.getparent()
    encodingDesc.remove(appinfoold)
    encodingDesc.insert(0, appInfo)

    revisionDesc = help_header.find("./mei:revisionDesc", namespaces=ns)
    del revisionDesc[1:]
    revisionDesc[0].attrib.update({"isodate": "YYYY-MM-DD", "n": "1", "resp": "#"})
    revisionDesc_ps = revisionDesc.xpath(".//mei:p", namespaces=ns)

    for revp in revisionDesc_ps:
        revp.text = ""

    root.remove(root.find("./mei:meiHead", namespaces=ns))
    root.insert(0, help_header)

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message


def get_last_mnum(root: etree.Element):
    """returns @n of last measure"""

    measures = root.xpath("//mei:measure", namespaces=ns)

    return int(measures[-1].get("n", "no_n"))


def add_foldir(measure: etree.Element, fol: str, tstamp: str):
    """
    Adds subelement dir to measure
    """
    dir = etree.SubElement(
        measure,
        "dir",
        {"staff": "1", "tstamp": tstamp, "place": "above", "type": "ref"},
    )
    rend = etree.SubElement(dir, "rend", {"fontstyle": "normal", "fontsize": "x-small"})
    rend.text = f"fol. {fol}"


def manual_unwrap(element):
    """
    Removes Element, adding children to parent

    :param element: etree.Element to be processed
    :type element: etree.Element
    """
    parent = element.getparent()
    if parent is None:
        return  # Cannot unwrap the root

    # Get the index of the element to maintain position
    index = parent.index(element)

    # Move children and handle text/tails
    previous = element.getprevious()
    if previous is not None:
        previous.tail = (previous.tail or "") + (element.text or "")
    else:
        parent.text = (parent.text or "") + (element.text or "")

    # Reverse iterate to keep indices stable during insertion
    for child in reversed(element):
        parent.insert(index, child)

    # Finally, remove the tag
    parent.remove(element)


def add_section_foldir_from_context_to_ed(
    active_dom: dict, context_doms: list, getElemFrom: str, **addargs
):
    """
    Removes expansion and section containing sections, adds foldir and sections/pbs based on dipl or ed

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param getElemFrom: string poitning to one notationtype in context_doms
    :type getElemFrom: str
    :param addargs: Addional arguments that are unused
    """
    output_message = ""

    if "ed" not in active_dom["notationtype"]:
        raise RuntimeError(f"{active_dom['filename']} must be ed_GLT or ed_CMN")

    root: etree.Element = active_dom["dom"]
    for context_dom in context_doms:
        if context_dom["notationtype"] == getElemFrom:
            help_dom = context_dom
            helproot = help_dom["dom"]
            break
    else:
        raise RuntimeError(
            f"add_section_foldir_from_dipl_GLT_to_ed needs context_dom {getElemFrom}, not found"
        )

    if "ed" in active_dom["notationtype"]:
        section_in_section = root.xpath(".//mei:section//mei:section", namespaces=ns)

        expansion = root.find(".//mei:expansion", namespaces=ns)
        if expansion is not None:
            expansion.getparent().remove(expansion)
            for s in section_in_section:
                manual_unwrap(s)
        elif section_in_section:
            RuntimeError(
                f"{active_dom["filename"]} contains recursive section but no expansion"
            )

    sections = root.xpath(".//mei:section", namespaces=ns)

    if len(sections) != 1:
        raise RuntimeError(f"{active_dom["filename"]} number of section is not 1")
    else:
        section = sections[0]

    if "dipl" in help_dom["notationtype"]:
        section_info = get_section_info_dipl(help_dom)
    else:
        if get_last_mnum(root) == get_last_mnum(helproot):
            section_info = get_section_info_ed(help_dom)
        else:
            raise RuntimeError(
                f"{active_dom["filename"]} has diffrent number of measures from {help_dom["filename"]}"
            )

    surfaces = root.xpath("//mei:facsimile/mei:surface", namespaces=ns)
    if len(surfaces) != len(section_info):
        raise RuntimeError(
            f"surfaces don't match number of sections in {active_dom["filename"]}"
        )

    surface_id = [f"#{s.get(f'{{{ns['xml']}}}id')}" for s in surfaces]

    score = section.getparent()

    section_info = [
        (
            mnum,
            fol,
            tstamp,
            etree.SubElement(score, "section", {"n": fol, "facs": facs}),
        )
        for (mnum, fol, tstamp), facs in zip(section_info, surface_id)
    ]

    section_children = list(section)
    current_n = ""
    current_section = -1  # current section statrs before 0 to account for first section
    print(section_info)
    for child in section_children:
        if child.tag == f"{{{ns['mei']}}}measure":
            current_n = child.get("n", "")
            if (
                current_section != len(section_info) - 1
                and current_n
                == section_info[current_section + 1][0]  # compare to next section mnum
            ):
                current_section += 1
                section_info[current_section][3].append(child)
                add_foldir(
                    child,
                    section_info[current_section][1],
                    section_info[current_section][2],
                )
            else:
                section_info[current_section][3].append(child)  # sh
        else:
            section_info[current_section][3].append(child)

    if current_section != len(section_info) - 1:
        raise RuntimeError(
            f"{active_dom["filename"]} wasn't processed because of misalignment of section"
        )

    print("succes!!")

    score.remove(section)

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message


def get_section_info_dipl(help_dom: dict):
    """
    Searches for dirs with folio to find mnum of measures after or containing page beginning
    """
    pb_measures = help_dom["dom"].xpath(".//mei:dir[@type='ref']/..", namespaces=ns)
    print([p.tag for p in pb_measures])

    if len(pb_measures) == 0:
        raise RuntimeError(f"No foldir in {help_dom["filename"]}")

    section_info = []

    for pb_measure in pb_measures:
        if pb_measure.tag != f"{{{ns['mei']}}}measure":
            raise RuntimeError(
                f"foldir parent wasn't measure in {help_dom["filename"]} at {pb_measure.get("n","no_n_found")}"
            )
        if pb_measure.xpath(
            "ancestor::mei:orig", namespaces=ns
        ):  # only look in choice/reg
            continue
        if pb_measure.xpath("ancestor::mei:reg", namespaces=ns):
            pbs = pb_measure.getparent().xpath("./mei:pb", namespaces=ns)
            if len(pbs) != 1:
                raise RuntimeError(
                    f"More than one pb in reg around {pb_measure.get("n","no_n_found")}"
                )
            pb = pbs[0]
        else:
            pb = get_previous_at_same_depth(help_dom["dom"], pb_measure)
            if pb is None or pb.tag != f"{{{ns['mei']}}}pb":
                raise RuntimeError(
                    f"No pb found before {pb_measure.get("n","no_n_found")}"
                )
        foldir = pb_measure.find("./mei:dir[@type='ref']", namespaces=ns)
        tstamp = foldir.get("tstamp")
        section_info.append((pb_measure.get("n"), pb.get("n"), tstamp))
    return section_info


def get_section_info_ed(help_dom: dict):
    """Searches for sections and dirs with folio denoting page peginning"""
    help_sections = help_dom["dom"].xpath("//mei:section[@n]", namespaces=ns)

    section_info = []
    for help_section in help_sections:
        help_measures = help_section.xpath(".//mei:measure", namespaces=ns)
        foldir = help_measures[0].xpath("./mei:dir[@type='ref']", namespaces=ns)
        if not foldir:
            raise RuntimeError(f"no foldir found in {help_dom["filename"]}")
        tstamp = foldir[0].get("tstamp")
        section_info.append((help_measures[0].get("n"), help_section.get("n"), tstamp))

    return section_info


def get_previous_at_same_depth(tree, element):
    """Finds previous sibling independnet of nesting"""
    depth = get_depth(element)
    same_depth = [el for el in tree.iter() if get_depth(el) == depth]
    idx = same_depth.index(element)
    return same_depth[idx - 1] if idx > 0 else None


def guess_string_width(text):
    """width of ascii letters as arbitary heuristic"""
    if not text:
        return 0

    # Split by lines and find the longest one (visual width-wise)
    lines = text.splitlines()

    # Character weight map based on a typical sans-serif font
    # Normalized so that a standard lowercase letter or space is ~1.0
    weights = {
        "wide": "MWm@W",  # Weight: 1.5
        "narrow": "ilj|!:.tI[]",  # Weight: 0.4
        "caps": "ABCDEFGHJKLNPQRSTUVXYZ",  # Weight: 1.2
    }

    def get_line_width(line):
        """nested function to compute line width with heuristic"""
        width = 0.0
        for char in line:
            if char in weights["wide"]:
                width += 1.5
            elif char in weights["narrow"]:
                width += 0.4
            elif char.isupper():
                width += 1.2
            elif char == "\t":
                width += 4.0  # Standard 4-space tab
            else:
                width += 1.0  # Default for lowercase, numbers, and spaces
        return width

    return max(get_line_width(line) for line in lines)


def add_finis_to_last_measure(
    active_dom: dict, context_doms: list, finisText: str, **addargs
):
    """
    adds finis on the last tstamp+1 of the last measure, doesnt account for mark-up or n-tuplets
    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param finisText: finis text
    :type finisText: str
    :param addargs: Addional arguments that are unused
    """
    output_message = ""

    if finisText == "":
        output_message = "No finis added because none given"
        return active_dom, output_message

    root = active_dom["dom"]

    meterSig = root.find(".//mei:meterSig", namespaces=ns)

    # remove before adding again
    finis = root.xpath("//mei:dir[@type='finis']", namespaces=ns)
    if finis:
        raise RuntimeError("Finis already there")

    measure = root.xpath("//mei:measure", namespaces=ns)[-1]
    layer = measure.find(".//mei:layer", namespaces=ns)
    tstamp = dur_length(layer)
    print(tstamp)
    if meterSig is not None:
        tstamp = tstamp * int(meterSig.get("unit", "4"))
    else:
        tstamp *= 4
        for _ in range(10):
            if tstamp.is_integer():
                break
            tstamp *= 2
        else:
            raise RuntimeError(
                f"Measure {measure.get("n","n_not_found")} has problematic tstamp calculation"
            )

    print(tstamp)
    vu = str(
        round(guess_string_width(finisText) * 2.5) + 5
    )  # vu to whitespace ca. 2.5 for wordlength + 5 as spacing
    dir = etree.SubElement(
        measure,
        "dir",
        {
            "staff": "2" if "ed_CMN" in active_dom["notationtype"] else "1",
            "tstamp": str(tstamp + 1),
            "place": "above" if "ed_CMN" in active_dom["notationtype"] else "within",
            "type": "finis",
            "ho": vu + "vu",
        },
    )
    rend = etree.SubElement(dir, "rend", {"halign": "right"})
    rend.text = finisText

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message
