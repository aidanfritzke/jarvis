"""What the human does in their editor: type an explanation into a placeholder."""

import os
import re
import sys

PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
import comments  # noqa: E402


def wrap(text, width=88):
    out = []
    for para in text.strip().split("\n"):
        line = ""
        for word in para.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        out.append(line)
    return out


_PREFIX = re.compile(r"^\s*(#+|//+|--+|;+|\*+|/\*+|\*/|<!--|-->)\s?")


def plain(text):
    """Comment text as prose: no comment syntax, no marker lines (model learners add both)."""
    lines = []
    for line in text.strip().split("\n"):
        if comments.MARKER_RE.search(line):
            continue
        prev = None
        while prev != line:
            prev, line = line, _PREFIX.sub("", line, count=1)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def fill_comment(repo, rel, cid, text):
    """Replace the body of chunk `cid`'s comment block with `text`."""
    text = plain(text)
    path = os.path.join(repo, rel)
    with open(path) as f:
        lines = f.read().split("\n")
    lang = comments.language_for(rel)
    marker = next(m for m in comments.find_markers(lines, lang) if m.cid == cid)
    first = lines[marker.start]
    indent = first[:len(first) - len(first.lstrip())]
    wrapped = wrap(text)
    if marker.style == "block" and marker.opener[0] == "/*":
        body = [indent + " * " + l if l else indent + " *" for l in wrapped] + [indent + " */"]
    elif marker.style == "block":
        body = [indent + l for l in wrapped] + [indent + marker.opener[1]]
    else:
        prefix = marker.opener
        body = [indent + prefix + " " + l if l else indent + prefix for l in wrapped]
    lines[marker.start + 1:marker.end + 1] = body
    with open(path, "w") as f:
        f.write("\n".join(lines))
