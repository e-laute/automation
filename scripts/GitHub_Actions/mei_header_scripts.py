import copy
import re

from lxml import etree
from pathlib import Path
from utils import dur_length, get_depth

ns = {
    "mei": "http://www.music-encoding.org/ns/mei",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


# ---------------------------------------------------------------------------
# static lookup tables / constants
# ---------------------------------------------------------------------------
 
NOTATION_EXPANSIONS = {
    "CMN": "Common Music Notation",
    "GLT": "German Lute Tablature",
    "ILT": "Italian Lute Tablature",
    "FLT": "French Lute Tablature",
}
 
EDITORIAL_GUIDELINES_TEXT = (
    "E-LAUTE Edition Guidelines: "
    "https://edition.onb.ac.at/fedora/objects/o:lau.red-editionguidelines/"
    "datastreams/MEI_CONVENTIONS/content"
)
 
# TODO: replace with real lookup of the meiHead template in the repository
# (e.g. one template per notation type, or a single shared template).
DEFAULT_TEMPLATE_PATH = Path("caller-repo") / "templates" / "meiHead_template.mei"
 
 
# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
 
def _replace_first_comment(container, text):
    """
    Find the first XML comment anywhere below `container` and replace it with `text`,
    preserving any text that was already there (e.g. the "o:lau." prefix before the
    "<!-- E-LAUTE ID -->" placeholder in <identifier type="PID">o:lau.<!-- E-LAUTE ID --></identifier>).
 
    :return: True if a comment was found and replaced, False otherwise.
    """
    comment = None
    for node in container.iter():
        if node.tag is etree.Comment:
            comment = node
            break
    if comment is None:
        return False
 
    parent = comment.getparent()
    tail = comment.tail or ""
 
    if parent is container and list(container).index(comment) == 0:
        # comment is the container's first child -> keep/extend container.text
        container.text = (container.text or "") + text + tail
    else:
        prev = comment.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + text + tail
        else:
            parent.text = (parent.text or "") + text + tail
 
    parent.remove(comment)
    return True
 
def _parse_template(path):
    try:
        return etree.parse(str(path), etree.XMLParser(recover=True))
    except OSError as e:
        raise RuntimeError(f"File error for {path}: {e}") from e
    except etree.XMLSyntaxError as e:
        raise RuntimeError(f"Parse error for {path}: {e}") from e


def _load_template_head(template_path: Path):
    """
    Load and return a *copy* of the <meiHead> element from the (mei-rooted) template file.
 
    The actual repository lookup (e.g. choosing a template based on notation
    type / source) is intentionally left out for now (see TODO above);
    callers may pass an explicit path via addargs["template_path"].
    """
    if template_path is None:
        template_path = DEFAULT_TEMPLATE_PATH
    try:
        template_tree = _parse_template(template_path)
    except RuntimeError:
        if template_path == DEFAULT_TEMPLATE_PATH:
            raise
    template_tree = _parse_template(DEFAULT_TEMPLATE_PATH)
    template_root = template_tree.getroot()
        
    new_head = template_root.find(".//mei:meiHead", namespaces=ns)
    if new_head is None:
        raise RuntimeError(f"Template file '{template_path}' has no mei:meiHead element.")
 
    return copy.deepcopy(new_head)
 
 
def _split_filename(filename, filetype):
    """
    Split a filename of the form
        (rism-code)_n(id)_(folio)(_line)?_enc_(filetype).mei
    into its idcombo ("A-Wn_Mus.Hs._18688_n05"), folio, and (optional) line parts.
 
    `filetype` is the "(ed|dipl)_(CMN|GLT|ILT|FLT)" string as found in
    active_dom["filetype"], reused here instead of re-deriving it from the
    filename.
 
    :return: (idcombo, folio, line) or None if the filename does not match.
    """
    suffix = f"_enc_{filetype}.mei"
    if not filename.endswith(suffix):
        return None
 
    prefix = filename[: -len(suffix)]
    parts = prefix.split("_n", 1)
    if len(parts) != 2:
        return None
    rism_part, rest = parts
 
    # `rest` now looks like "<id>_<folio>" or "<id>_<folio>_<line>"
    rest_parts = rest.split("_")
    if len(rest_parts) < 2 or not rest_parts[0].isdigit():
        return None
 
    id_number = rest_parts[0]
    folio = rest_parts[1]
    line = "_".join(rest_parts[2:]) if len(rest_parts) > 2 else None
 
    idcombo = f"{rism_part}_n{id_number}"
    return idcombo, folio, line
 
 
# ---------------------------------------------------------------------------
# main function
# ---------------------------------------------------------------------------
 
def add_header_from_template(active_dom: dict, context_doms: list, templatePath: str, **addargs):
    """
    Replace the meiHead of the active document with the meiHead of a template file.
 
    The following information is carried over / derived while doing so:
      - encodingDesc/appInfo of the *old* meiHead is preserved (the template's
        empty appInfo is discarded)
      - the old meiHead's title text is written into the new title[@type='main']
      - based on active_dom["filetype"] ("(ed|dipl)_(CMN|GLT|ILT|FLT)") and the
        filename (rism-code)_n(id)_(folio)(_line)?_enc_(filetype).mei:
          * title[@type='main']/titlePart[@type='subordinate'] gets
            "transcription in " / "edition in " + the notation-type <abbr>
          * editionStmt/edition text is rewritten
          * the two "<!-- E-LAUTE ID -->" identifier comments (pubStmt PID and
            sourceDesc/.../analytic/identifier) are replaced with the combined
            rism-code + individual identifier (e.g. "A-Wn_Mus.Hs._18688_n05")
          * analytic/biblScope is set to the folio (and, if present, line) info
      - editorialDecl's first <p> comment is replaced with a link to the
        E-LAUTE edition guidelines
 
    :param active_dom: dict containing {filename:Path/str?, notationtype:str, filetype:str, dom:etree.Element}
    :type active_dom: dict
    :param context_doms: list containing dom dicts
    :type context_doms: list
    :param addargs: Additional arguments (recognised: "template_path")
    """
    output_message = ""
    summary_message = ""
 
    root = active_dom["dom"]
 
    old_head = root.find(".//mei:meiHead", namespaces=ns)
    if old_head is None:
        raise RuntimeError("No meiHead found in the active document - aborting.")
 
    # -----------------------------------------------------------------
    # 1. load template meiHead
    # -----------------------------------------------------------------
    new_head = _load_template_head(Path(templatePath) if templatePath else None)
 
    # -----------------------------------------------------------------
    # 2. preserve old appInfo, drop the template's empty one
    # -----------------------------------------------------------------
    old_app_info = old_head.find(".//mei:encodingDesc/mei:appInfo", namespaces=ns)
    new_encoding_desc = new_head.find(".//mei:encodingDesc", namespaces=ns)
    if new_encoding_desc is not None:
        template_app_info = new_encoding_desc.find("mei:appInfo", namespaces=ns)
        if template_app_info is not None:
            new_encoding_desc.remove(template_app_info)
        if old_app_info is not None:
            new_encoding_desc.append(copy.deepcopy(old_app_info))
    elif old_app_info is not None:
        output_message += "Warning: template has no encodingDesc - could not carry over appInfo. "
 
    # -----------------------------------------------------------------
    # 3. old title text -> new title[@type='main']
    # -----------------------------------------------------------------
    old_title_text = ""
    for title_el in old_head.findall(".//mei:title", namespaces=ns):
        if title_el.text and title_el.text.strip():
            old_title_text = title_el.text.strip()
            break
 
    new_title_main = new_head.find(".//mei:title[@type='main']", namespaces=ns)
    if new_title_main is not None:
        new_title_main.text = old_title_text
 
    # -----------------------------------------------------------------
    # 4. filetype / filename-based adjustments
    # -----------------------------------------------------------------
    filetype = active_dom.get("filetype", "")  # e.g. "dipl_GLT"
    edtype, _, notation = filetype.partition("_")
 
    filename = active_dom.get("filename", "")
    filename = Path(filename).name if filename else ""
 
    split = _split_filename(filename, filetype) if filename and filetype else None
 
    if not filetype or edtype not in ("ed", "dipl") or notation not in NOTATION_EXPANSIONS:
        output_message += (
            f"Warning: active_dom['filetype'] ('{filetype}') is missing or malformed; "
            "skipped filetype-based header adjustments. "
        )
    else:
        is_edition = edtype == "ed"
        edition_label = "edition" if is_edition else "diplomatic transcription"
        preposition_phrase = "edition in" if is_edition else "transcription in"
 
        # 4a. title[@type='main']/titlePart + abbr
        if new_title_main is not None:
            title_part = new_title_main.find("mei:titlePart[@type='subordinate']", namespaces=ns)
            if title_part is not None:
                title_part.text = f"{preposition_phrase} "
                abbr = title_part.find("mei:abbr", namespaces=ns)
                if abbr is not None:
                    abbr.text = notation
                    abbr.set("expan", NOTATION_EXPANSIONS[notation])
 
        # 4b. editionStmt/edition text
        edition_el = new_head.find(".//mei:edition", namespaces=ns)
        if edition_el is not None:
            if notation == "CMN":
                first_sentence = f"First {edition_label} in CMN."
            else:
                first_sentence = f"First {edition_label}."
            edition_el.text = f"{first_sentence} Lute tuned in A."
 
        # 4c./4d. identifiers and biblScope need idcombo/folio/line from the filename
        if split is None:
            output_message += (
                f"Warning: filename '{filename}' did not match the expected naming "
                f"pattern for filetype '{filetype}'; skipped identifiers and biblScope. "
            )
        else:
            idcombo, folio, line = split
 
            for identifier_el in new_head.findall(".//mei:identifier", namespaces=ns):
                _replace_first_comment(identifier_el, idcombo)
 
            bibl_scope = new_head.find(".//mei:biblScope", namespaces=ns)
            if bibl_scope is not None:
                folio_text = f"{folio}_{line}" if line else folio
                _replace_first_comment(bibl_scope, folio_text)
 
    # -----------------------------------------------------------------
    # 5. editorialDecl - first <p> placeholder comment
    # -----------------------------------------------------------------
    editorial_decl = new_head.find(".//mei:editorialDecl", namespaces=ns)
    if editorial_decl is not None:
        editorial_ps = editorial_decl.findall("mei:p", namespaces=ns)
        if editorial_ps:
            _replace_first_comment(editorial_ps[0], EDITORIAL_GUIDELINES_TEXT)
 
    # -----------------------------------------------------------------
    # 6. swap the meiHead into the active document
    # -----------------------------------------------------------------
    parent = old_head.getparent()
    if parent is None:
        output_message += "Warning: old meiHead has no parent - replacing document root. "
        new_head.tail = old_head.tail
        root = new_head
    else:
        index = list(parent).index(old_head)
        new_head.tail = old_head.tail
        parent.remove(old_head)
        parent.insert(index, new_head)
 
    active_dom["dom"] = root
    summary_message = "meiHead replaced from template."
 
    return active_dom, output_message, summary_message

def add_header_from_context(
    active_dom: dict,
    context_doms: list,
    projectstaff: str,
    getElemFrom: str,
    **addargs,
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
        raise RuntimeError(
            f"{active_dom["filename"]} already has E-LAUTE header"
        )

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
    if (
        "ed" in active_dom["notationtype"]
        and "dipl" in help_dom["notationtype"]
    ):
        abbr.getparent().text = "edition in "
    elif (
        "CMN" in active_dom["notationtype"]
        and "GLT" in help_dom["notationtype"]
    ):
        abbr.clear()
        abbr.set("expan", "Common Music Notation")
        abbr.text = "CMN"
    elif (
        "dipl" in active_dom["notationtype"]
        and "ed" in help_dom["notationtype"]
    ):
        abbr.getparent().text = "transcription in "
    elif (
        "GLT" in active_dom["notationtype"]
        and "CMN" in help_dom["notationtype"]
    ):
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
    revisionDesc[0].attrib.update(
        {"isodate": "YYYY-MM-DD", "n": "1", "resp": "#"}
    )
    revisionDesc_ps = revisionDesc.xpath(".//mei:p", namespaces=ns)

    for revp in revisionDesc_ps:
        revp.text = ""

    root.remove(root.find("./mei:meiHead", namespaces=ns))
    root.insert(0, help_header)

    active_dom["dom"] = root
    summary_message = ""

    return active_dom, output_message, summary_message