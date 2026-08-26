import copy

from lxml import etree
from utils import dur_length

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
    in case a measure ever has more than 26 tabGrps of interest)."""
    i += 1
    out = ""
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = chr(97 + rem) + out
    return out


# ---------------------------------------------------------------------------
# xml:id generation
# ---------------------------------------------------------------------------

def _id_string_to_id_list(id_string:str):
    return id_string.split(",")

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
# main function
# ---------------------------------------------------------------------------

def orig_reg_sbs(active_dom: dict, context_doms: list, sbXmlId: str, addargs):
    """
    For each xml:id in `sbXmlId`, resolve it to a <tabGrp> (an id may point directly
    at a tabGrp or at any of its descendants) and treat the tabGrp as a "here the
    source's system actually breaks mid-measure" marker. For each affected measure,
    wrap it in <choice>: one side keeps the measure whole with an editorial <sb>
    added next to it ("reg", the regularized reading); the other side is a full copy
    of the measure, physically split into "<n>a", "<n>b", ... pieces at each marked
    tabGrp with an <sb> between each pair ("orig", the diplomatic reading showing the
    real break). Which side is the untouched original vs. the copy doesn't matter
    semantically - they end up structurally identical bar the ids - so the untouched
    measure is kept in "reg" (cheapest: no copying needed) and the copy is the one
    that gets split, in "orig".

    An ancestor element (staff/layer/beam/...) whose first child leads to the tabGrp
    is never duplicated as an empty shell in the earlier split piece - it's kept
    wholesale in the piece that actually contains the tabGrp. If a tabGrp is the
    very first content of its layer (nothing precedes it anywhere in the layer), the
    id is skipped with a warning, since there is nothing to split off. Ids that
    don't resolve to a tabGrp at all (not one, and not inside one) are also skipped
    with a warning.

    :param active_dom: dict containing {filename:Path/str?, notationtype:str, dom:etree.Element}
    :param context_doms: list containing dom dicts
    :param sbXmlId: list of xml:id strings marking tabGrps where a system break occurs
    :param addargs: additional arguments (unused)
    """
    output_message = ""
    root = active_dom["dom"]

    xml_ids = sbXmlId.split(",")

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

    # --- drop tabGrps that are the first content of their layer ----------------
    valid_tabgrps = []
    for tabgrp in resolved_tabgrps:
        is_first, _layer = _is_first_content_of_layer(tabgrp)
        if is_first:
            output_message += (
                f"Warning: tabGrp '{tabgrp.get(XML_ID)}' is the first content of its layer "
                "- nothing precedes it to split off, ignored.\n"
            )
            continue
        valid_tabgrps.append(tabgrp)

    if not valid_tabgrps:
        summary_message = "No tabGrps could be processed."
        active_dom["dom"] = root
        return active_dom, output_message, summary_message

    # --- group by ancestor measure, sort measures and tabGrps-within-measure ---
    # by document order, so a measure with several marked tabGrps is only ever
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

        # --- orig: a full copy of the measure, split at each marked tabGrp ---
        copy_measure = _deep_copy_with_appended_ids(measure, "-cp", used_ids)
        target_paths = [_path_from(measure, tabgrp) for tabgrp in tabgrps_in_measure]
        copy_targets = [_element_at_path(copy_measure, p) for p in target_paths]

        pieces = []
        current_tail = copy_measure
        for i, target in enumerate(copy_targets):
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
                sb_between = etree.Element(_mei("sb"))
                sb_between.set(XML_ID, _append_unique_id(measure_id, f"-sb{_letter(i)}", used_ids))
                orig_el.append(sb_between)

        # --- reg: the untouched original measure, with a single <sb> placed on
        # whichever side of it is closer to where the split(s) actually happen ---
        parent.remove(measure)
        reg_sb = etree.Element(_mei("sb"))
        reg_sb.set(XML_ID, _append_unique_id(measure_id, "-sb", used_ids))

        first_piece_dur = dur_length(pieces[0])
        rest_dur = sum(dur_length(p) for p in pieces[1:])
        if first_piece_dur > rest_dur:
            reg_el.append(measure)
            reg_el.append(reg_sb)
        else:
            reg_el.append(reg_sb)
            reg_el.append(measure)

        parent.insert(measure_index, choice_el)
        processed_count += len(tabgrps_in_measure)

    active_dom["dom"] = root
    summary_message = (
        f"Created {len(ordered_measures)} choice/orig/reg alternative(s) "
        f"covering {processed_count} tabGrp(s)."
    )
    return active_dom, output_message, summary_message