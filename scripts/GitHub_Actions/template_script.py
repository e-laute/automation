import os
import re
from lxml import etree
import copy
import sys
import math
from utils import *


ns = {"mei":"http://www.music-encoding.org/ns/mei",
        "xml":"http://www.w3.org/XML/1998/namespace"}

    

def function(root:etree.Element):
    """Function description.

    Args:
      root: The root of the parsed tree of the MEI-file.

    Returns:
      The changed root.
      Optional: The output string containing the formatted information.

    Raises:
      Error-type: Any potential Errors.
    """

    output_str = ""

    xpath_result = root.xpath(".//mei:elem[@attrib='value']", namespaces=ns)
    
    return root,output_str