"""Comment syntax and code structure for comments-by-humans.

Everything here is pure text processing with no state: which comment syntax a
file uses, how to find EXPLAIN(human) markers, where a chunk's code starts and
ends, and how to split an existing file into chunks for review mode.

The chunk extent rules are a heuristic that works across brace languages and
indentation languages without a parser:

* A chunk starts at the first non-blank line after its comment.
* It runs through everything nested inside that first statement (deeper
  indentation or open brackets), plus any line glued to it without a blank
  line in between.
* It stops at a blank line followed by a new statement at the same
  indentation, at a dedent below its own indentation (the enclosing scope
  ended), at a closing bracket that belongs to the enclosing scope, or at the
  next marker.
"""

import hashlib
import os
import re

MARKER_RE = re.compile(r"\b(EXPLAIN|EXPLAINED)\(human\)\s+([cr]\d{2,})\b")


class Lang:
    def __init__(self, name, line=(), block=None, quotes=("'", '"'), triple=(),
                 multiline_quotes=(), char_quote=False, shell_hash=False,
                 markup=False, continuations=(), alt_blocks=(), alt_line=()):
        self.name = name
        self.line = tuple(line)            # line comment prefixes
        self.block = block                 # (start, end) or None
        # Markup files embed scripts and styles, whose comments use other syntaxes.
        self.blocks = ([block] if block else []) + list(alt_blocks)
        self.lines = tuple(line) + tuple(alt_line)
        self.quotes = tuple(quotes)        # single-line string delimiters
        self.triple = tuple(triple)        # multi-line string delimiters, e.g. '"""'
        self.multiline_quotes = tuple(multiline_quotes)  # e.g. '`'
        self.char_quote = char_quote       # "'" starts a char literal, not a string
        self.shell_hash = shell_hash       # '#' only starts a comment at a word start
        self.markup = markup
        self.continuations = tuple(continuations)

    def placeholder(self, cid, indent="", block=None):
        """Lines of an empty placeholder for chunk `cid` (in `block` syntax when given)."""
        tag = "EXPLAIN(human) %s" % cid
        block = block or self.block
        if block and block[0] == "/*":
            return [indent + "/* " + tag, indent + " *", indent + " */"]
        if block:
            start, end = block
            return [indent + start + " " + tag, indent, indent + end]
        prefix = self.line[0]
        return [indent + prefix + " " + tag, indent + prefix]


_CLIKE_CONT = ("else", "catch", "finally", "while")
_PY_CONT = ("else", "elif", "except", "finally", "case")
_RUBY_CONT = ("else", "elsif", "rescue", "ensure", "end", "when", "in", "then")
_SH_CONT = ("else", "elif", "fi", "done", "esac", "then", "do", "}")
_LUA_CONT = ("else", "elseif", "end", "until")

C = Lang("c", line=("//",), block=("/*", "*/"), char_quote=True, continuations=_CLIKE_CONT)
JS = Lang("js", line=("//",), block=("/*", "*/"), multiline_quotes=("`",), continuations=_CLIKE_CONT)
GO = Lang("go", line=("//",), block=("/*", "*/"), multiline_quotes=("`",), char_quote=True,
          continuations=_CLIKE_CONT)
SWIFT = Lang("swift", line=("//",), block=("/*", "*/"), triple=('"""',), char_quote=True,
             continuations=_CLIKE_CONT)
DART = Lang("dart", line=("//",), block=("/*", "*/"), triple=('"""', "'''"), continuations=_CLIKE_CONT)
PHP = Lang("php", line=("//", "#"), block=("/*", "*/"), continuations=_CLIKE_CONT)
CSS = Lang("css", block=("/*", "*/"))
SCSS = Lang("scss", line=("//",), block=("/*", "*/"))
SQL = Lang("sql", line=("--",), block=("/*", "*/"), quotes=("'", '"'))
PY = Lang("python", line=("#",), triple=('"""', "'''"), continuations=_PY_CONT)
RUBY = Lang("ruby", line=("#",), continuations=_RUBY_CONT)
SH = Lang("shell", line=("#",), shell_hash=True, continuations=_SH_CONT)
HASH = Lang("hash", line=("#",))
R = Lang("r", line=("#",), continuations=("else",))
ELIXIR = Lang("elixir", line=("#",), triple=('"""',), continuations=("else", "end", "rescue", "after"))
LUA = Lang("lua", line=("--",), block=("--[[", "]]"), continuations=_LUA_CONT)
HASKELL = Lang("haskell", line=("--",), block=("{-", "-}"), quotes=('"',))
OCAML = Lang("ocaml", block=("(*", "*)"), quotes=('"',))
LISP = Lang("lisp", line=(";;", ";"), quotes=('"',))
HTML = Lang("html", block=("<!--", "-->"), markup=True, alt_blocks=(("/*", "*/"),), alt_line=("//",))
ERLANG = Lang("erlang", line=("%",), quotes=('"',))
MATLAB = Lang("matlab", line=("%",), quotes=("'", '"'), continuations=("else", "elseif", "end"))

EXTENSIONS = {
    ".c": C, ".h": C, ".cc": C, ".cpp": C, ".cxx": C, ".hpp": C, ".hh": C, ".hxx": C,
    ".m": C, ".mm": C, ".java": C, ".cs": C, ".kt": C, ".kts": C, ".scala": C, ".sc": C,
    ".rs": C, ".groovy": C, ".gradle": C, ".proto": C, ".zig": C, ".v": C, ".sol": C,
    ".js": JS, ".jsx": JS, ".mjs": JS, ".cjs": JS, ".ts": JS, ".tsx": JS, ".mts": JS, ".cts": JS,
    ".go": GO, ".swift": SWIFT, ".dart": DART, ".php": PHP,
    ".css": CSS, ".scss": SCSS, ".less": SCSS, ".sql": SQL,
    ".py": PY, ".pyi": PY, ".rb": RUBY, ".rake": RUBY,
    ".sh": SH, ".bash": SH, ".zsh": SH, ".fish": SH,
    ".pl": HASH, ".pm": HASH, ".tf": HASH, ".nix": HASH, ".cmake": HASH, ".jl": HASH,
    ".ps1": HASH, ".r": R, ".R": R, ".ex": ELIXIR, ".exs": ELIXIR,
    ".lua": LUA, ".hs": HASKELL, ".elm": HASKELL, ".ml": OCAML, ".mli": OCAML,
    ".clj": LISP, ".cljs": LISP, ".el": LISP, ".lisp": LISP, ".scm": LISP, ".rkt": LISP,
    ".html": HTML, ".htm": HTML, ".vue": HTML, ".svelte": HTML,
    ".erl": ERLANG, ".hrl": ERLANG,
}
FILENAMES = {
    "Makefile": HASH, "makefile": HASH, "GNUmakefile": HASH, "Dockerfile": HASH,
    "Containerfile": HASH, "Rakefile": RUBY, "Gemfile": RUBY, "Justfile": HASH, "justfile": HASH,
    "CMakeLists.txt": HASH, "BUILD": HASH, "WORKSPACE": HASH, "Vagrantfile": RUBY,
}
SHEBANGS = (("python", PY), ("node", JS), ("bash", SH), ("sh", SH), ("zsh", SH), ("ruby", RUBY),
            ("perl", HASH))


def language_for(path, text=None):
    """The Lang for a path, or None when the file type is unknown."""
    base = os.path.basename(path)
    if base in FILENAMES:
        return FILENAMES[base]
    if base.startswith("Dockerfile"):
        return HASH
    ext = os.path.splitext(base)[1]
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    if ext.lower() in EXTENSIONS:
        return EXTENSIONS[ext.lower()]
    if not ext and text and text.startswith("#!"):
        first = text.split("\n", 1)[0]
        for needle, lang in SHEBANGS:
            if needle in first:
                return lang
    return None


# ---------------------------------------------------------------------------
# Lexing


class LineInfo:
    __slots__ = ("code", "starts_inside", "ends_inside", "has_comment")

    def __init__(self, code, starts_inside, ends_inside, has_comment):
        self.code = code                    # line with comments removed, strings collapsed to ""
        self.starts_inside = starts_inside  # began inside a multi-line string or comment
        self.ends_inside = ends_inside
        self.has_comment = has_comment


_CHAR_LITERAL = re.compile(r"'(\\.[^']{0,8}|[^'\\])'")


def lex(lines, lang):
    """Strip comments and string contents line by line, tracking state across lines."""
    out = []
    state = None  # ("block", end) or ("string", delim, escapes)
    for line in lines:
        starts_inside = state is not None
        code = []
        has_comment = False
        i, n = 0, len(line)
        while i < n:
            if state is not None:
                if state[0] == "block":
                    j = line.find(state[1], i)
                    if j < 0:
                        i = n
                        break
                    i = j + len(state[1])
                    state = None
                    continue
                delim = state[1]
                j = i
                found = -1
                while j < n:
                    if line[j] == "\\":
                        j += 2
                        continue
                    if line.startswith(delim, j):
                        found = j
                        break
                    j += 1
                if found < 0:
                    i = n
                    break
                i = found + len(delim)
                state = None
                continue
            ch = line[i]
            opened = next((b for b in lang.blocks if line.startswith(b[0], i)), None)
            if opened:
                has_comment = True
                state = ("block", opened[1])
                i += len(opened[0])
                continue
            hit = False
            for prefix in lang.lines:
                if line.startswith(prefix, i):
                    if lang.shell_hash and i > 0 and not line[i - 1].isspace():
                        continue
                    hit = True
                    break
            if hit:
                has_comment = True
                break
            triple = next((t for t in lang.triple if line.startswith(t, i)), None)
            if triple:
                code.append('""')
                state = ("string", triple)
                i += len(triple)
                continue
            if ch in lang.multiline_quotes:
                code.append('""')
                state = ("string", ch)
                i += 1
                continue
            if ch in lang.quotes:
                if ch == "'" and lang.char_quote:
                    m = _CHAR_LITERAL.match(line, i)
                    if m:
                        code.append("''")
                        i = m.end()
                    else:
                        code.append(ch)  # lifetime or apostrophe, not a string
                        i += 1
                    continue
                j = i + 1
                while j < n and line[j] != ch:
                    j += 2 if line[j] == "\\" else 1
                code.append('""')
                i = j + 1
                continue
            code.append(ch)
            i += 1
        out.append(LineInfo("".join(code), starts_inside, state is not None, has_comment))
    return out


def _indent(line):
    expanded = line.replace("\t", "    ")
    return len(expanded) - len(expanded.lstrip(" "))


def bracket_delta(code):
    return sum(code.count(c) for c in "{[(") - sum(code.count(c) for c in "}])")


# ---------------------------------------------------------------------------
# Markers


class Marker:
    __slots__ = ("cid", "tag", "start", "end", "indent", "body", "style", "opener")

    def __init__(self, cid, tag, start, end, indent, body, style, opener):
        self.cid = cid
        self.tag = tag        # "EXPLAIN" or "EXPLAINED"
        self.start = start    # first line of the comment block (0-based)
        self.end = end        # last line of the comment block (0-based, inclusive)
        self.indent = indent
        self.body = body      # the human's text, comment syntax stripped
        self.style = style    # "block" or "line"
        self.opener = opener  # (start, end) for a block comment, the prefix for line comments

    @property
    def explained(self):
        return self.tag == "EXPLAINED"


def _comment_open(stripped, lang):
    for block in lang.blocks:
        if stripped.startswith(block[0]):
            return "block", block
    for prefix in sorted(lang.lines, key=len, reverse=True):
        if stripped.startswith(prefix):
            return "line", prefix
    return None, None


def _strip_comment_syntax(text, style, opener):
    lines = []
    for raw in text.split("\n"):
        s = raw.strip()
        if style == "block":
            start, end = opener
            if s.startswith(start):
                s = s[len(start):]
            if s.endswith(end):
                s = s[: -len(end)]
            s = s.strip()
            if start == "/*":
                s = s.lstrip("*").strip()
        elif s.startswith(opener):
            s = s[len(opener):].strip()
        lines.append(s)
    return "\n".join(lines).strip()


def find_markers(lines, lang):
    """All EXPLAIN/EXPLAINED markers that open a comment block, in file order."""
    markers = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = MARKER_RE.search(line)
        stripped = line.strip()
        style, opener = _comment_open(stripped, lang) if m else (None, None)
        if not m or not style or stripped.index(m.group(0)) > 6:
            i += 1
            continue
        end = i
        if style == "block":
            after = line[line.find(opener[0]) + len(opener[0]):]
            if opener[1] not in after:
                j = i + 1
                while j < n and opener[1] not in lines[j]:
                    j += 1
                end = min(j, n - 1)
        else:
            j = i + 1
            while (j < n and lines[j].strip().startswith(opener)
                   and not MARKER_RE.search(lines[j])):
                j += 1
            end = j - 1
        text = "\n".join(lines[i:end + 1])
        text = text.replace(m.group(0), "", 1)
        markers.append(Marker(m.group(2), m.group(1), i, end, _indent(line),
                              _strip_comment_syntax(text, style, opener), style, opener))
        i = end + 1
    return markers


def word_count(text):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", text))


def set_marker_tag(lines, marker, tag):
    """Return lines with the marker's tag replaced (EXPLAIN <-> EXPLAINED)."""
    new = list(lines)
    new[marker.start] = new[marker.start].replace(
        "%s(human) %s" % (marker.tag, marker.cid), "%s(human) %s" % (tag, marker.cid), 1)
    return new


# ---------------------------------------------------------------------------
# Extents


def _is_continuation(stripped, lang):
    if lang.markup and stripped.startswith("</"):
        return True
    word = re.match(r"[A-Za-z_]+", stripped)
    return bool(word) and word.group(0) in lang.continuations


def _starts_with_closer(stripped):
    return stripped[:1] in ("}", ")", "]")


def extent(lines, info, start, lang, marker_lines=frozenset()):
    """Last line (inclusive) of the unit that begins at `start`."""
    n = len(lines)
    base = _indent(lines[start])
    depth = 0
    end = start
    prev_blank = False
    for i in range(start, n):
        text = lines[i]
        li = info[i]
        if li.starts_inside and i > start:
            depth += bracket_delta(li.code)
            end = i
            prev_blank = False
            continue
        stripped = text.strip()
        if not stripped:
            prev_blank = True
            continue
        if i > start:
            ind = _indent(text)
            if i in marker_lines and ind <= base:
                break
            if depth <= 0:
                code = li.code.strip() or stripped
                if ind < base:
                    break
                if ind == base:
                    if _starts_with_closer(code) and not lang.markup:
                        break
                    if not _is_continuation(code, lang) and prev_blank:
                        break
        depth += bracket_delta(li.code)
        end = i
        prev_blank = False
    return end


def chunk_regions(lines, lang, info=None, markers=None):
    """Map chunk id -> (marker, code_start, code_end); code_start is None for an empty chunk."""
    info = info if info is not None else lex(lines, lang)
    markers = markers if markers is not None else find_markers(lines, lang)
    marker_lines = frozenset(m.start for m in markers)
    regions = {}
    for m in markers:
        start = m.end + 1
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start >= len(lines) or start in marker_lines:
            regions[m.cid] = (m, None, None)
            continue
        regions[m.cid] = (m, start, extent(lines, info, start, lang, marker_lines))
    return regions


def region_hash(lines, start, end, markers):
    """Hash of a chunk's code, ignoring nested markers' comments so approvals don't ripple."""
    if start is None:
        return hashlib.sha256(b"").hexdigest()
    skip = set()
    for m in markers:
        if start <= m.start <= end:
            skip.update(range(m.start, m.end + 1))
    body = "\n".join(lines[i].rstrip() for i in range(start, end + 1) if i not in skip)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Lines that need no chunk


_IMPORT_RE = re.compile(
    r"""^\s*(
        import\b | from\s+\S+\s+import\b | export\s+(\*|\{[^}]*\}|type\s+\{[^}]*\})\s+from\b |
        \#\s*(include|import|pragma|define|ifndef|ifdef|endif|if|else|elif|undef)\b |
        using\b | package\b | use\b | require(_relative)?\b | extern\s+crate\b | mod\s+\w+\s*; |
        @import\b | @use\b | @forward\b | library\b | part\b | namespace\s+[\w.\\]+\s*; |
        module\s+[\w.]+\s*(where|;|$) | (const|let|var)\s+[\w{},\s]+=\s*require\( |
        ["'](use\s+strict|use\s+client|use\s+server)["'];? | __all__\b | set\s+-[euxo]
    )""", re.VERBOSE)


def needs_no_chunk(lines, info, lang):
    """Per-line flags: True where a line is blank, a comment, or import-like boilerplate."""
    flags = []
    import_depth = 0
    for i, line in enumerate(lines):
        li = info[i]
        code = li.code.strip()
        if not line.strip() or (not code and (li.has_comment or li.starts_inside)):
            flags.append(True)
            continue
        if line.startswith("#!") and i == 0:
            flags.append(True)
            continue
        if import_depth > 0:
            import_depth = max(0, import_depth + bracket_delta(code))
            flags.append(True)
            continue
        if _IMPORT_RE.match(line):
            import_depth = max(0, bracket_delta(code))
            flags.append(True)
            continue
        if code in ('""', "''") and lang is PY:  # module docstring
            flags.append(True)
            continue
        flags.append(False)
    return flags


# ---------------------------------------------------------------------------
# Units for review mode


def units(lines, lang, target=40, lo=0, hi=None, depth=0):
    """Split lines[lo:hi] into complete units (functions, classes, statement groups)."""
    if lang.markup:
        return _markup_units(lines, lang, target)
    info = lex(lines, lang)
    return _units(lines, info, lang, target, lo, len(lines) if hi is None else hi, depth)


_EMBED_OPEN = re.compile(r"<(script|style)\b[^>]*>\s*$", re.I)


def embedded_regions(lines):
    """(first, last, lang) for the contents of multi-line <script> and <style> blocks."""
    regions = []
    i = 0
    while i < len(lines):
        m = _EMBED_OPEN.search(lines[i])
        if m:
            close = "</%s>" % m.group(1).lower()
            j = i + 1
            while j < len(lines) and close not in lines[j].lower():
                j += 1
            if j > i + 1:
                regions.append((i + 1, j - 1, JS if m.group(1).lower() == "script" else CSS))
            i = j
        i += 1
    return regions


def embedded_block(lines, index):
    """The comment syntax at a line of a markup file: C-style inside <script>/<style>."""
    for first, last, _ in embedded_regions(lines):
        if first <= index <= last:
            return ("/*", "*/")
    return None


def _markup_units(lines, lang, target):
    found = []
    inside = set()
    for first, last, sub in embedded_regions(lines):
        inside.update(range(first - 1, last + 2))
        info = lex(lines, sub)
        found.extend(_units(lines, info, sub, target, first, last + 1, 1))
    info = lex(lines, lang)
    free = needs_no_chunk(lines, info, lang)
    run = []
    for i in range(len(lines) + 1):
        if i < len(lines) and i not in inside and lines[i].strip():
            run.append(i)
            continue
        if sum(1 for k in run if not free[k]) >= 3:
            found.append([run[0], run[-1]])
        run = []
    return sorted(found)


def _units(lines, info, lang, target, lo, hi, depth):
    free = needs_no_chunk(lines, info, lang)
    markers = find_markers(lines, lang)
    in_marker = set()
    for m in markers:
        in_marker.update(range(m.start, m.end + 1))
    found = []
    i = lo
    while i < hi:
        if free[i] or i in in_marker:
            i += 1
            continue
        end = min(extent(lines, info, i, lang), hi - 1)
        # A unit made only of comments (a header block) is not code.
        if all(free[k] for k in range(i, end + 1)):
            i = end + 1
            continue
        found.append([i, end])
        i = end + 1
    # Split a large container (a class or namespace) into its members.
    result = []
    for start, end in found:
        if end - start + 1 > 2 * target and depth < 3:
            inner = _units(lines, info, lang, target, start + 1, end + 1, depth + 1)
            inner = [u for u in inner if u[0] > start]
            if len(inner) >= 2:
                inner[0][0] = start
                inner[-1][1] = end
                result.extend(inner)
                continue
        result.append([start, end])
    # Group runs of small statements (constants, short helpers) up to the target, and fold a
    # lone tiny statement into its neighbor so no chunk is a single constant.
    def near(a, b):
        return b[0] - a[1] <= 3 and _indent(lines[a[0]]) == _indent(lines[b[0]])

    def size(u):
        return u[1] - u[0] + 1

    grouped = []
    for unit in result:
        if grouped:
            last = grouped[-1]
            if size(unit) <= 5 and last[2] and size(last) <= target - size(unit) and near(last, unit):
                last[1] = unit[1]
                continue
        grouped.append([unit[0], unit[1], size(unit) <= 5])
    folded = []
    k = 0
    while k < len(grouped):
        unit = grouped[k]
        nxt = grouped[k + 1] if k + 1 < len(grouped) else None
        if size(unit) <= 2 and nxt and near(unit, nxt) and size(unit) + size(nxt) <= target:
            nxt[0] = unit[0]
        elif size(unit) <= 2 and folded and near(folded[-1], unit) and \
                size(folded[-1]) + size(unit) <= target:
            folded[-1][1] = unit[1]
        else:
            folded.append(unit)
        k += 1
    return [[s, e] for s, e, _ in folded]


# ---------------------------------------------------------------------------
# Names, for review ordering


_DEF_PATTERNS = [
    r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)",
    r"^\s*class\s+([A-Za-z_]\w*)",
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)",
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
    r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|type|const|static|mod)\s+([A-Za-z_]\w*)",
    r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:]+\s+for\s+)?([A-Za-z_]\w*)",
    r"^\s*(?:(?:public|private|protected|internal|static|final|override|virtual|abstract|async|"
    r"synchronized|suspend|open|inline)\s+)+[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*\(",
    r"^(?:[A-Za-z_][\w*<>:,]*\s+)+\**([A-Za-z_]\w*)\s*\(",
    r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=[^=]",
    r"^\s*(?:def|defp|defmodule)\s+([A-Za-z_][\w.!?]*)",
    r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)",
    r"^([A-Za-z_][\w-]*)\s*\(\)\s*\{",
]
_DEF_RES = [re.compile(p) for p in _DEF_PATTERNS]
_NOT_NAMES = {"if", "for", "while", "switch", "return", "sizeof", "catch", "else", "new", "await",
              "yield", "throw", "delete", "typeof", "self", "this", "super", "main"}
_IDENT = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*")  # attribute names after "." are not references
_ENTRY_RE = re.compile(
    r"__name__\s*==\s*['\"]__main__['\"]|^\s*(?:pub\s+)?(?:async\s+)?(?:def|func|fn|function)\s+main\b|"
    r"\bstatic\s+void\s+main\s*\(|^\s*int\s+main\s*\(|^\s*export\s+default\b|\bapp\.listen\(|"
    r"\bmain\(\)\s*$", re.M)


def defined_names(lines, info, start, end):
    base = _indent(lines[start])
    names = set()
    for i in range(start, end + 1):
        if not lines[i].strip() or _indent(lines[i]) > base + 4:
            continue
        for rx in _DEF_RES:
            m = rx.match(lines[i])
            if m and _indent(lines[i]) <= base + 4:
                names.add(m.group(1).split(".")[-1].split(":")[-1])
    return {n for n in names if len(n) > 1 and n not in _NOT_NAMES}


def referenced_names(info, start, end):
    names = set()
    for i in range(start, end + 1):
        names.update(_IDENT.findall(info[i].code))
    return names


_LOCAL_RES = [re.compile(p) for p in (
    r"(?<![\w.])([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=(?!=)",
    r"\bfor\s+\(?\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)",
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
    r"\bas\s+([A-Za-z_]\w*)",
    r"\bexcept\s+[\w.]+\s+as\s+([A-Za-z_]\w*)",
)]
_PARAMS = re.compile(r"\b(?:def|function|func|fn)\s*\*?\s*[\w$]*\s*\(([^)]*)\)")


def local_names(info, start, end):
    """Names a chunk binds itself (assignments, loop variables, parameters), so not dependencies."""
    names = set()
    first_indent = None
    for i in range(start, end + 1):
        code = info[i].code
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip())
        if first_indent is None:
            first_indent = indent
        for m in _PARAMS.finditer(code):
            names.update(re.findall(r"([A-Za-z_$][\w$]*)\s*(?=[:=,)]|$)", m.group(1)))
        if indent == first_indent and i == start:
            continue  # the chunk's own top-level definition is a def, not a local
        for rx in _LOCAL_RES:
            names.update(m.group(1) for m in rx.finditer(code))
    return names


def is_entry_point(lines, start, end):
    return bool(_ENTRY_RE.search("\n".join(lines[start:end + 1])))
