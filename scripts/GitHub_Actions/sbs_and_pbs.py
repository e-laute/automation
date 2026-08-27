import copy
import re

from lxml import etree
from utils import dur_length, dur_to_tstamp

ns = {
    "mei": "http://www.music-encoding.org/ns/mei",
    "xml": "http://www.w3.org/XML/1998/namespace",
}
XML_ID = f"{{{ns['xml']}}}id"


def _mei(tag):
    return f"{{{ns['mei']}}}{tag}"


def _localname(el):
    return etree.QName(el).localname


# ---------------------------------------------------------------------------
# lookup / traversal helpers
# ---------------------------------------------------------------------------

def _find_by_id(root, xml_id):
    result = root.xpath(f".//*[@xml:id='{xml_id}']", namespaces=ns)
    return result[0] if result else None


def _nearest_ancestor(el, tagname):
    node = el.getparent()
    while node is not None:
        if _localname(node) == tagname:
            return node
        node = node.getparent()
    return None


def _resolve_to_tabgrp(el):
    """
    `el` itself if it is a tabGrp; its nearest tabGrp ancestor if it's nested inside
    one (e.g. a <note> or <accid> inside a <tabGrp>); otherwise None.
    """
    if _localname(el) == "tabGrp":
        return el
    return _nearest_ancestor(el, "tabGrp")


def _is_first_content_of_layer(tabgrp):
    """
    True if `tabgrp` (or the unbroken chain of first-children leading to it) is the
    very first piece of content in its ancestor <layer> - i.e. there is nothing
    before it in the layer that a split could meaningfully separate off.
    Returns (is_first, layer_el_or_None).
    """
    layer = _nearest_ancestor(tabgrp, "layer")
    if layer is None:
        return True, None  # no layer ancestor at all - nothing sensible to split
    node = tabgrp
    while node is not layer:
        parent = node.getparent()
        if list(parent).index(node) != 0:
            return False, layer
        node = parent
    return True, layer


def _ancestors_between(target, stop):
    """Ancestors of `target` strictly between `target` (exclusive) and `stop`
    (exclusive), ordered outer -> inner."""
    chain = []
    node = target
    while True:
        parent = node.getparent()
        if parent is stop:
            break
        if parent is None:
            raise ValueError("`stop` is not an ancestor of `target`")
        chain.append(parent)
        node = parent
    chain.reverse()
    return chain


def _path_from(ancestor, descendant):
    """Child-index path from `ancestor` down to `descendant` (list of ints)."""
    path = []
    node = descendant
    while node is not ancestor:
        parent = node.getparent()
        path.append(list(parent).index(node))
        node = parent
    path.reverse()
    return path


def _element_at_path(el, path):
    node = el
    for i in path:
        node = list(node)[i]
    return node


def _letter(i):
    """0 -> 'a', 1 -> 'b', ..., 25 -> 'z', 26 -> 'aa', ... (spreadsheet-column style,
    in case a measure ever has more than 26 marked positions)."""
    i += 1
    out = ""
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = chr(97 + rem) + out
    return out


# ---------------------------------------------------------------------------
# xml:id generation
# ---------------------------------------------------------------------------

def _append_unique_id(base_id, suffix, used_ids):
    """Build a new xml:id by appending `suffix` to `base_id`, disambiguating
    further (by appending an incrementing number) in the unlikely case that's
    already taken."""
    candidate = f"{base_id}{suffix}"
    n = 2
    while candidate in used_ids:
        candidate = f"{base_id}{suffix}{n}"
        n += 1
    used_ids.add(candidate)
    return candidate


def _deep_copy_with_appended_ids(el, suffix, used_ids):
    """Deep-copy `el`; every xml:id found in the copy gets `suffix` appended so the
    copy's ids never collide with the original's (or each other's)."""
    new_el = copy.deepcopy(el)
    for node in new_el.iter():
        old_id = node.get(XML_ID)
        if old_id is not None:
            node.set(XML_ID, _append_unique_id(old_id, suffix, used_ids))
    return new_el


def _shallow_copy_new_id(el, suffix, used_ids):
    """Shallow copy of `el` (tag + attributes, no children) with a fresh xml:id
    derived by appending `suffix` to the original id. Used for the ancestor levels
    (measure/staff/layer/...) that get 'closed and reopened' by a split."""
    new_el = etree.Element(el.tag, attrib=el.attrib)
    old_id = el.get(XML_ID)
    if old_id is not None:
        new_el.set(XML_ID, _append_unique_id(old_id, suffix, used_ids))
    return new_el


# ---------------------------------------------------------------------------
# the actual split
# ---------------------------------------------------------------------------

def _find_split_boundary(container, chain, target):
    """
    Walk from `target` outward to `container`, looking for the first ancestor level
    (starting from the innermost) that is *not* the first child of its parent -
    that's the level with genuine content to split off.

    :return: (boundary_index, boundary_child). boundary_index indexes into `chain`
        (that element's children get partitioned); -1 means `container` itself is
        that level. (None, None) if `target` is fully "pass-through" up to
        `container` (nothing anywhere above it to split off).
    """
    nodes = chain + [target]
    parents = [container] + chain
    for i in range(len(nodes) - 1, -1, -1):
        node, parent = nodes[i], parents[i]
        if list(parent).index(node) != 0:
            return i - 1, node
    return None, None


def _split_off_before(container, target, letter, used_ids):
    """
    Split `container` (a measure, or a previously split-off tail piece shaped like
    one) into (before_piece, container) at the point just before `target`.

    `container` is mutated in place into the "tail": it keeps `target` and
    everything from it onward, with its existing xml:ids untouched. `before_piece`
    is newly built, containing whatever preceded `target`, with fresh xml:ids (each
    the duplicated wrapper's id + "-{letter}").

    Any ancestor level whose first child leads to `target` is *not* duplicated -
    it stays entirely inside `container`/the tail (e.g. a <beam> that starts right
    at the tabGrp doesn't get an empty copy in the "before" piece).
    """
    chain = _ancestors_between(target, container)
    boundary_index, boundary_child = _find_split_boundary(container, chain, target)
    if boundary_index is None:
        raise ValueError("nothing precedes `target` up to `container` - should have been filtered earlier")

    boundary_container = container if boundary_index == -1 else chain[boundary_index]
    idx = list(boundary_container).index(boundary_child)
    before_children = list(boundary_container)[:idx]
    for child in before_children:
        boundary_container.remove(child)

    levels = [container] + (chain[: boundary_index + 1] if boundary_index >= 0 else [])
    new_top = new_parent = None
    for level_el in levels:
        new_el = _shallow_copy_new_id(level_el, f"-{letter}", used_ids)
        if new_top is None:
            new_top = new_el
        else:
            new_parent.append(new_el)
        new_parent = new_el
    for child in before_children:
        new_parent.append(child)

    return new_top


# ---------------------------------------------------------------------------
# shared core for orig_reg_sbs / add_pbs_id
# ---------------------------------------------------------------------------

def _place_breaks(active_dom: dict, xml_ids: list[str], tag: str, skip_first_content: bool):
    """
    Shared implementation behind `orig_reg_sbs` (tag="sb", skip_first_content=True)
    and `add_pbs_id` (tag="pb", skip_first_content=False). See their docstrings for
    the user-facing behaviour; the only things that differ between the two are the
    element tag inserted, and what happens when a marked position is the first
    content of its layer: `orig_reg_sbs` has nothing meaningful to split off there
    and skips it with a warning, while `add_pbs_id` instead just inserts a bare
    <pb/> directly before the measure (a page turn exactly at a measure boundary
    needs no orig/reg alternative at all).
    """
    
    if "dipl" not in active_dom["notationtype"]:
        raise RuntimeError(f"{active_dom["filename"]} is not dipl")
    output_message = ""
    root = active_dom["dom"]
    staffDef = root.find("mei:staffDef",namespaces=ns)
    if staffDef is None or not staffDef.get("notationtype","").startswith("tab.lute"):
        raise RuntimeError(f"{active_dom["filename"]} is not tablature")
    used_ids = set(root.xpath(".//@xml:id", namespaces=ns))
    doc_order = {el: i for i, el in enumerate(root.iter())}

    # --- resolve each requested id to a tabGrp, deduplicating -----------------
    resolved_tabgrps = []
    seen = set()
    for xml_id in xml_ids:
        el = _find_by_id(root, xml_id)
        if el is None:
            output_message += f"Warning: no element with xml:id '{xml_id}' found - ignored.\n"
            continue
        tabgrp = _resolve_to_tabgrp(el)
        if tabgrp is None:
            output_message += (
                f"Warning: xml:id '{xml_id}' is not a tabGrp and is not inside one - ignored.\n"
            )
            continue
        if id(tabgrp) in seen:
            continue
        seen.add(id(tabgrp))
        resolved_tabgrps.append(tabgrp)

    # --- handle tabGrps that are the first content of their layer --------------
    valid_tabgrps = []
    for tabgrp in resolved_tabgrps:
        is_first, _layer = _is_first_content_of_layer(tabgrp)
        if not is_first:
            valid_tabgrps.append(tabgrp)
            continue

        if skip_first_content:
            output_message += (
                f"Warning: tabGrp '{tabgrp.get(XML_ID)}' is the first content of its layer "
                "- nothing precedes it to split off, ignored.\n"
            )
            continue

        # insert a bare break directly before the measure - no choice/orig/reg needed
        measure = _nearest_ancestor(tabgrp, "measure")
        parent = measure.getparent() if measure is not None else None
        if parent is None:
            output_message += (
                f"Warning: tabGrp '{tabgrp.get(XML_ID)}' has no usable ancestor measure - ignored.\n"
            )
            continue
        bare_break = etree.Element(_mei(tag))
        bare_break.set(XML_ID, _append_unique_id(measure.get(XML_ID) or tag, f"-{tag}", used_ids))
        parent.insert(list(parent).index(measure), bare_break)

    if not valid_tabgrps:
        raise RuntimeError(f"No mid-measure {tag}s could be processed.")

    # --- group by ancestor measure, sort measures and tabGrps-within-measure ---
    # by document order, so a measure with several marked positions is only ever
    # split once (into N+1 sequentially-lettered pieces), not repeatedly.
    measure_groups = {}
    for tabgrp in valid_tabgrps:
        measure = _nearest_ancestor(tabgrp, "measure")
        if measure is None:
            output_message += (
                f"Warning: tabGrp '{tabgrp.get(XML_ID)}' has no ancestor measure - ignored.\n"
            )
            continue
        measure_groups.setdefault(measure, []).append(tabgrp)

    ordered_measures = sorted(measure_groups.keys(), key=lambda m: doc_order[m])

    processed_count = 0
    for measure in ordered_measures:
        tabgrps_in_measure = sorted(measure_groups[measure], key=lambda t: doc_order[t])

        parent = measure.getparent()
        if parent is None:
            output_message += f"Warning: measure '{measure.get(XML_ID)}' has no parent - skipped.\n"
            continue
        measure_index = list(parent).index(measure)
        measure_id = measure.get(XML_ID) or "measure"

        # --- scaffolding ---
        choice_el = etree.Element(_mei("choice"))
        choice_el.set(XML_ID, _append_unique_id(measure_id, "-ch", used_ids))
        orig_el = etree.SubElement(choice_el, _mei("orig"))
        orig_el.set(XML_ID, _append_unique_id(measure_id, "-orig", used_ids))
        reg_el = etree.SubElement(choice_el, _mei("reg"))
        reg_el.set(XML_ID, _append_unique_id(measure_id, "-reg", used_ids))

        # --- orig: the original, split at each marked position ---
        copy_measure = _deep_copy_with_appended_ids(measure, "-cp", used_ids)

        pieces = []
        current_tail = measure
        for i, target in enumerate(tabgrps_in_measure):
            pieces.append(_split_off_before(current_tail, target, _letter(i), used_ids))
        pieces.append(current_tail)

        base_n = measure.get("n", "")
        for i, piece in enumerate(pieces):
            piece.set("n", f"{base_n}{_letter(i)}")
            piece.set("metcon", "false")
            if i < len(pieces) - 1:
                piece.set("right", "invis")
            orig_el.append(piece)
            if i < len(pieces) - 1:
                break_between = etree.Element(_mei(tag))
                break_between.set(XML_ID, _append_unique_id(measure_id, f"-{tag}{_letter(i)}", used_ids))
                orig_el.append(break_between)

        # --- reg: a deep copy of the original measure, with a single break placed on
        # whichever side of it is closer to where the split(s) actually happen ---
        parent.remove(measure)
        reg_break = etree.Element(_mei(tag))
        reg_break.set(XML_ID, _append_unique_id(measure_id, f"-{tag}", used_ids))

        first_piece_dur = dur_length(pieces[0])
        rest_dur = sum(dur_length(p) for p in pieces[1:])
        if first_piece_dur > rest_dur:
            reg_el.append(copy_measure)
            reg_el.append(reg_break)
        else:
            reg_el.append(reg_break)
            reg_el.append(copy_measure)

        parent.insert(measure_index, choice_el)
        processed_count += len(tabgrps_in_measure)

    active_dom["dom"] = root
    summary_message = (
        f"Created {len(ordered_measures)} choice/orig/reg alternative(s) "
        f"covering {processed_count} {tag}(s)."
    )
    return active_dom, output_message, summary_message


# ---------------------------------------------------------------------------
# public functions
# ---------------------------------------------------------------------------

def orig_reg_sbs(active_dom: dict, context_doms: list, sbXmlId: str, **addargs):
    """
    For each xml:id in `sbXmlId`, resolve it to a <tabGrp> (an id may point directly
    at a tabGrp or at any of its descendants) and treat the tabGrp as a "here the
    source's system actually breaks mid-measure" marker. For each affected measure,
    wrap it in <choice>: one side keeps the measure whole with an editorial <sb>
    added next to it ("reg", the regularized reading); the other side is a full copy
    of the measure, physically split into "<n>a", "<n>b", ... pieces at each marked
    tabGrp with an <sb> between each pair ("orig", the diplomatic reading showing the
    real break).

    If a marked tabGrp is the very first content of its layer (nothing precedes it
    anywhere in the layer), it is skipped with a warning, since there is nothing to
    split off. Ids that don't resolve to a tabGrp at all are also skipped with a
    warning.

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :param context_doms: list containing dom dicts
    :param sbXmlId: string of xml:id seperated by comma marking tabGrps where a system break occurs
    :param addargs: additional arguments (unused)
    """
    return _place_breaks(active_dom, sbXmlId.split(","), tag="sb", skip_first_content=True)


def add_pbs_id(active_dom: dict, context_doms: list, sbXmlId: str, **addargs):
    """
    Place a page-break marker (<pb/>) at the position identified by `sbXmlId` (an id
    may point directly at a tabGrp or at any of its descendants).

    If the position falls in the middle of a measure, it is treated exactly like
    `orig_reg_sbs`: the measure is wrapped in <choice>, split into orig pieces with
    a <pb> between them, and reg keeps the whole measure with a single <pb> placed
    on whichever side is closer to the split point.

    If the position is the first content of its layer (i.e. effectively at the very
    start of the measure), no orig/reg alternative is needed - a bare <pb/> is
    simply inserted directly before that measure.

    Note this only places the <pb> itself; it does not fill in @n, @facs, or add the
    accompanying <dir> - see `fill_pb_info` for that (a separate pass, so pbs added
    by hand also get picked up).

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :param context_doms: list containing dom dicts
    :param sbXmlId: string of xml:id seperated by comma marking where the page break occurs
    :param addargs: additional arguments (unused)
    """
    return _place_breaks(active_dom, sbXmlId.split(","), tag="pb", skip_first_content=False)


# ---------------------------------------------------------------------------
# fill_pb_info: separate pass filling in @n / @facs / <dir> for every <pb>
# ---------------------------------------------------------------------------

def _folio_index(folio):
    match = re.match(r"^(\d+)([rv])$", folio)
    if not match:
        raise RuntimeError(f"'{folio}' is not a valid folio (expected e.g. '6r' or '7v').")
    num, side = int(match.group(1)), match.group(2)
    return num * 2 + (0 if side == "r" else 1)


def _folio_range(start, end=None):
    """
    List of folio labels from `start` to `end` inclusive (e.g. "6r" .. "7v" ->
    ["6r", "6v", "7r", "7v"]). Just [start] if `end` is None. Raises RuntimeError if
    `end` is not strictly after `start`.
    """
    if end is None:
        return [start]
    start_idx = _folio_index(start)
    end_idx = _folio_index(end)
    if end_idx <= start_idx:
        raise RuntimeError(f"End folio '{end}' is not after start folio '{start}'.")
    return [f"{i // 2}{'r' if i % 2 == 0 else 'v'}" for i in range(start_idx, end_idx + 1)]


def _measure_after(pb):
    """The <measure> immediately following `pb` (pbs are always inserted as a
    sibling directly before the measure where the new page/system begins)."""
    sib = pb.getnext()
    return sib if sib is not None and _localname(sib) == "measure" else None


def _add_dir(measure, tstamp, folio, used_ids):
    dir_el = etree.SubElement(measure, _mei("dir"))
    dir_el.set("type", "ref")
    dir_el.set("place", "above")
    dir_el.set("staff", "1")
    dir_el.set("tstamp", str(tstamp))
    dir_el.set("size", "xx-small")
    dir_el.set(XML_ID, _append_unique_id(measure.get(XML_ID) or "measure", "-dir", used_ids))
    dir_el.text = f"fol. {folio}"
    return dir_el


def fill_pb_info(active_dom: dict, context_doms: list, **addargs):
    """
    Fill in @n and @facs for every <pb> in the document (however it got there - this
    is a separate pass from `add_pbs_id` precisely so hand-added pbs are picked up
    too), and add the corresponding <dir> annotation.

    Folios are derived from active_dom["filename"] (a "<n>r"/"<n>v" or
    "<n>r-<n>v" pattern) and matched positionally, in document order, against the
    <surface> elements of the document's <facsimile> - this assumes each file's
    facsimile fragment already covers exactly this file's folio range, one surface
    per folio side.

    The first folio/surface belongs to the opening page, which needs its own <pb>
    as the very first child of <score> (created if not already present) - the rest
    of the pbs found in the document (in order) consume the remaining folios/
    surfaces one by one.

    For a <pb> that sits inside a <choice>'s <orig>, the corresponding <pb> in that
    choice's <reg> gets the same @n/@facs. A <dir> is added to the measure right
    after the orig <pb> (at tstamp="1", since the new page starts right at that
    measure), and to the end of the (single, unsplit) reg measure, with @tstamp
    derived from the duration of the first orig piece (via dur_length +
    dur_to_tstamp). If a choice ever has more than one <pb> in orig or in reg (only
    possible if multiple positions were marked within the same measure), only the
    first is processed - the rest are left untouched with a warning, since that
    multi-break-per-measure case is already a simplified edge case upstream.

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :param context_doms: list containing dom dicts
    :param addargs: additional arguments (unused)
    """
    
    if "dipl" not in active_dom["notationtype"]:
        raise RuntimeError(f"{active_dom["filename"]} is not dipl")
    output_message = ""
    root = active_dom["dom"]

    surfaces = root.xpath(".//mei:facsimile//mei:surface[@xml:id]", namespaces=ns)
    if not surfaces:
        raise RuntimeError("No <facsimile> with a <surface xml:id=...> found - nothing to fill in.")

    filename = active_dom.get("filename", "")
    match = re.search(r"(\d+[rv])(?:-(\d+[rv]))?", filename)
    if not match:
        raise RuntimeError(f"Could not find a folio pattern in filename '{filename}' - aborting.")


    folios = _folio_range(match.group(1), match.group(2))

    if len(surfaces) != len(folios):
        raise RuntimeError(
            f"Number of surfaces ({len(surfaces)}) does not match the number of folios "
            f"({len(folios)}) derived from the filename - aborting."
        )

    used_ids = set(root.xpath(".//@xml:id", namespaces=ns))
    doc_order = {el: i for i, el in enumerate(root.iter())}

    section = root.find(".//mei:section",namespaces=ns)
    for child in section.iter():
        if _localname(child) == "pb":
            break
        if _localname(child) == "measure":
            section.insert(0,etree.Element("pb"))

    # --- group the remaining pbs by their enclosing choice (if any) ------------
    choices = {}  # choice_el -> {"orig": [...], "reg": [...]}
    bare_pbs = []
    for pb in root.findall(".//mei:pb", namespaces=ns):
        choice = _nearest_ancestor(pb, "choice")
        if choice is None:
            bare_pbs.append(pb)
            continue
        bucket = "orig" if _nearest_ancestor(pb, "orig") is not None else "reg"
        choices.setdefault(choice, {"orig": [], "reg": []})[bucket].append(pb)
    for groups in choices.values():
        groups["orig"].sort(key=lambda p: doc_order[p])
        groups["reg"].sort(key=lambda p: doc_order[p])

    # a choice contributes at most one orig pb (and one reg pb) to the assignment;
    # any additional ones are left unfilled with a warning
    primary_choice_pbs = {}  # choice_el -> its single orig pb
    for choice, groups in choices.items():
        if len(groups["orig"]) > 1:
            output_message += (
                f"Warning: choice '{choice.get(XML_ID)}' has {len(groups['orig'])} pbs in orig "
                "- only the first is processed, the rest are left unfilled.\n"
            )
        if len(groups["reg"]) > 1:
            output_message += (
                f"Warning: choice '{choice.get(XML_ID)}' has {len(groups['reg'])} pbs in reg "
                "- only the first is processed, the rest are left unfilled.\n"
            )
        if groups["orig"]:
            primary_choice_pbs[choice] = groups["orig"][0]

    # --- assign @n / @facs: bare pbs + the (one) orig pb per choice consume the
    # remaining folios/surfaces, in document order ---
    primary = sorted(bare_pbs + list(primary_choice_pbs.values()), key=lambda p: doc_order[p])

    n = min(len(primary), len(folios))
    if len(primary) > n:
        output_message += f"Warning: {len(primary) - n} pb(s) have no folio/surface left - left unfilled.\n"
    elif len(folios) > n:
        output_message += f"Warning: {len(folios) - n} folio(s)/surface(s) have no pb to assign to - unused.\n"

    assigned = {}
    for pb, folio, surface in zip(primary, folios[:n], surfaces[:n]):
        pb.set("n", folio)
        pb.set("facs", f"#{surface.get(XML_ID)}")
        assigned[pb] = folio

    # --- bare pbs: dir on the following measure, tstamp=1 -----------------------
    for pb in bare_pbs:
        if pb not in assigned:
            continue
        measure = _measure_after(pb)
        if measure is None:
            output_message += f"Warning: pb '{pb.get(XML_ID)}' has no following measure - dir not added.\n"
            continue
        _add_dir(measure, 1, assigned[pb], used_ids)

    # --- choice-grouped pbs ------------------------------------------------------
    for choice, orig_pb in primary_choice_pbs.items():
        if orig_pb not in assigned:
            continue

        reg_pbs = choices[choice]["reg"]
        if reg_pbs:
            reg_pbs[0].set("n", orig_pb.get("n"))
            reg_pbs[0].set("facs", orig_pb.get("facs"))

        orig_el = choice.find("mei:orig", namespaces=ns)
        reg_el = choice.find("mei:reg", namespaces=ns)
        first_orig_piece = orig_el.find("mei:measure", namespaces=ns) if orig_el is not None else None
        reg_measure = reg_el.find("mei:measure", namespaces=ns) if reg_el is not None else None

        measure = _measure_after(orig_pb)
        if measure is None:
            output_message += f"Warning: orig pb '{orig_pb.get(XML_ID)}' has no following measure - dir not added.\n"
        else:
            _add_dir(measure, 1, assigned[orig_pb], used_ids)

        if reg_measure is None:
            output_message += f"Warning: choice '{choice.get(XML_ID)}' has no reg measure - reg dir not added.\n"
        elif first_orig_piece is None:
            output_message += f"Warning: choice '{choice.get(XML_ID)}' has no orig measure - reg dir not added.\n"
        else:
            tstamp = dur_to_tstamp(dur_length(first_orig_piece),root.find(".//mei:meterSig",namespaces=ns))
            _add_dir(reg_measure, tstamp, assigned[orig_pb], used_ids)

    active_dom["dom"] = root
    summary_message = f"Filled in {len(assigned) + 1} pb(s) against {len(folios)} folio(s)."
    return active_dom, output_message, summary_message