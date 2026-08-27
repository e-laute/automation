import os

from lxml import etree
from datetime import date

ns = {
    "mei": "http://www.music-encoding.org/ns/mei",
    "xml": "http://www.w3.org/XML/1998/namespace",
}


def write_to_console(message: str):
    # might get more complicated!
    print(format_user_output(message))


def write_to_github_summary(message: str):

    with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
        f.write(message)


def format_user_output(
    message: str,
):  # to create string according to GH-Action standard
    return (
        "::group::Application Results\n"
        "---START_USER_MESSAGE---\n"
        f"{message}\n"
        "---END_USER_MESSAGE---\n"
        "::endgroup::\n"
    )


# TODO: move function from derive-alternate-tablature-notation-types.py here?
def edit_appInfo(root: etree.Element, p_description: str):
    """Adds `<p>` containing p_description to `<application>` with `<name>` GitHub action Script under `<appInfo>`.

    Args:
      root: The root of the parsed tree of the MEI-file.
      p_description: String to be added to a `<p>`-elem under `<application>`.

    Returns:
      The changed root.

    Raises:
      Error-type: Any potential Errors.
    """

    applications = root.xpath(
        ".//mei:application/mei:name[normalize-space(.)='GitHub Action Scripts']/..",
        namespaces=ns,
    )

    if not applications:
        app_Info = root.find(".//mei:appInfo", namespaces=ns)
        application = etree.SubElement(
            app_Info, "application", {"isodate": date.today().isoformat()}
        )
        name = etree.SubElement(application, "name")
        name.text = "GitHub Action Scripts"
    else:
        application = applications[0]
        if (
            application.get("isodate") is not None
            and application.get("isodate") != date.today().isoformat()
        ):
            application.set("startdate", application.get("isodate"))
            application.attrib.pop("isodate")
        if application.get("isodate") is None:
            application.set("enddate", date.today().isoformat())

    p = etree.SubElement(application, "p")
    p.text = p_description

    return root


def get_depth(element):
    return sum(1 for _ in element.iterancestors())


def dur_length(elem: etree.Element, ignore=["sic", "orig"]):
    """Recursively adds up all `@dur` in subtree while accounting for `@dots`.

    Args:
      elem: Root of a MEI-Subtree.
      ignore (optional): List of elements to not count; defaults to orig and sic to avoid choice duplication

    Returns:
      Float which represents the combined dur in Quavers.

    Raises:
      Error-type: Any potential Errors.
    """

    totaldur = 0.0
    for child in elem:
        if etree.QName(child).localname in ignore:
            continue
        if "dur" in child.attrib:
            dur = float(child.attrib.get("dur"))
            totaldur += 2 / dur - 1 / (
                dur * 2 ** int(child.attrib.get("dots", "0"))
            )
        else:
            totaldur += dur_length(child)
    return totaldur

def dur_to_tstamp(dur: float, meterSig: etree.Element):
    
    if meterSig is not None:
        tstamp = dur * int(meterSig.get("unit", "4"))
    else:
        tstamp = dur * 4
        for _ in range(10):
            if tstamp.is_integer():
                break
            tstamp *= 2
        else:
            raise RuntimeError(
                "Could not calculate tstamp"
            )