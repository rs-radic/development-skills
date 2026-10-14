"""Deterministic ClickUp comment-shape parser (plan r5 §2.2, §7.3).

Only a positively recognized plain-text shape can carry a command. There is no
text-equality fallback for quoted or unknown shapes.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

PLAIN = "PLAIN"
QUOTED = "QUOTED"
UNKNOWN = "UNKNOWN"

# Part attributes that mark quoted or code-formatted text.
QUOTE_ATTR_KEYS = frozenset({"blockquote", "code-block", "code", "quote"})

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Parsed:
    norm_text: str
    lines: list
    mentions: set = field(default_factory=set)
    shape: str = UNKNOWN
    nontext_parts: int = 0
    shape_sig: str = ""


def normalize(s):
    """Collapse whitespace, trim, casefold and drop one optional trailing period."""
    s = _WS.sub(" ", s).strip().casefold()
    if s.endswith("."):
        s = s[:-1].rstrip()
    return s


def _shape_sig(parts):
    types, keys = set(), set()
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, dict):
            types.add(str(part.get("type", "text")))
            attrs = part.get("attributes")
            if isinstance(attrs, dict):
                keys.update(str(k) for k in attrs)
        else:
            types.add("<" + type(part).__name__ + ">")
    blob = json.dumps({"types": sorted(types), "attrs": sorted(keys)}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _tag_user_id(part):
    user = part.get("user")
    if not isinstance(user, dict):
        return None
    uid = user.get("id")
    if isinstance(uid, bool):
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def parse_comment(parts, comment_text, allowlist):
    """Classify a ClickUp comment ``comment[]`` part list and its ``comment_text``."""
    sig = _shape_sig(parts)
    unknown = Parsed(norm_text="", lines=[], mentions=set(), shape=UNKNOWN, nontext_parts=0, shape_sig=sig)
    if not isinstance(parts, list) or not isinstance(comment_text, str):
        return unknown
    allowed = set(allowlist or ())

    full, stripped = [], []  # concatenation with and without tag parts
    mentions, nontext, recognized, quoted = set(), 0, True, False
    for part in parts:
        if not isinstance(part, dict):
            recognized = False
            continue
        ptype = part.get("type")
        attrs = part.get("attributes")
        if attrs is None:
            attrs = {}
        if not isinstance(attrs, dict):
            recognized = False
            attrs = {}
        if any(k in QUOTE_ATTR_KEYS for k in attrs):
            quoted = True
        if ptype is not None and ptype != "text":
            nontext += 1
        text = part.get("text", "")
        if not isinstance(text, str):
            recognized = False
            continue
        if any(k not in allowed and k not in QUOTE_ATTR_KEYS for k in attrs):
            recognized = False
        if ptype is None or ptype == "text":
            if "text" not in part:
                recognized = False
            full.append(text)
            stripped.append(text)
        elif ptype == "tag":
            uid = _tag_user_id(part)
            if uid is None:
                recognized = False
            else:
                mentions.add(uid)
            full.append(text)
        else:
            recognized = False

    body = "".join(stripped)
    lines = [ln for ln in (normalize(raw) for raw in body.splitlines()) if ln]
    raw_lines = body.splitlines() + comment_text.splitlines()
    # A quote marker wins over unrecognized parts/attributes: quoted text is context only.
    if quoted or any(ln.lstrip().startswith(">") for ln in raw_lines):
        shape = QUOTED
    elif not recognized:
        return Parsed(norm_text="", lines=[], mentions=mentions, shape=UNKNOWN, nontext_parts=nontext, shape_sig=sig)
    elif normalize(comment_text) == normalize("".join(full)):
        shape = PLAIN
    else:
        shape = UNKNOWN
    return Parsed(norm_text=normalize(body), lines=lines, mentions=mentions, shape=shape,
                  nontext_parts=nontext, shape_sig=sig)
