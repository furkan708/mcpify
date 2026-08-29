"""Response format conversion (stdlib only).

`--format auto` converts XML responses to JSON on the fly; `--format xml`
forces conversion whenever the body parses as XML. The mapping is
deterministic and intentionally simple:

- attributes become "@name" keys
- repeated child elements become lists
- element text lands under "value" when an element has both text and
  children/attributes
- namespaces are stripped (the local name survives; prefixes do not)

This is a convenience for agents, not a lossless XML tool — documents
that need schema-accurate XML handling should stay raw.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET


def xml_to_dict(element: ET.Element) -> dict | str:
    node: dict = {}
    for key, value in element.attrib.items():
        node["@" + key.split("}")[-1]] = value
    children = list(element)
    if children:
        grouped: dict[str, list] = {}
        for child in children:
            name = child.tag.split("}")[-1]
            grouped.setdefault(name, []).append(xml_to_dict(child))
        for name, values in grouped.items():
            node[name] = values[0] if len(values) == 1 else values
        text = (element.text or "").strip()
        if text:
            node["value"] = text
        return node
    text = (element.text or "").strip()
    if not node:
        return text
    if text:
        node["value"] = text
    return node


def looks_like_xml(body: str, content_type: str = "") -> bool:
    if "xml" in (content_type or "").lower():
        return True
    head = body.lstrip()[:200]
    return head.startswith("<") and not head.startswith("<!")


def convert(body: str, parsed_json: object, content_type: str, fmt: str) -> tuple[str, object]:
    """Return (text_for_the_model, structured_or_None) after optional
    conversion. fmt: auto | json | xml.

    `auto` trusts the transport header only (Content-Type says xml) — a
    JSON-declared endpoint returning HTML garbage is upstream breakage,
    and silently converting it would mask the failure. `xml` converts on
    best effort regardless of the header, because the user asked."""
    if fmt not in ("auto", "xml"):
        return body, parsed_json
    if parsed_json is not None:
        return body, parsed_json
    if fmt == "auto" and "xml" not in (content_type or "").lower():
        return body, None
    if not looks_like_xml(body, content_type):
        return body, None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        if fmt == "xml":
            return body + "\n\n[xml conversion failed: body is not well-formed XML]", None
        return body, None
    data = {root.tag.split("}")[-1]: xml_to_dict(root)}
    return json.dumps(data, ensure_ascii=False, indent=2), data
