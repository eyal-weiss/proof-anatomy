#!/usr/bin/env python3
"""Proof Anatomy, pass 1: read every .lean file as text (no build, no LLM) and write one record per file.

Per-file record (JSON lines, gzip):
  p   path relative to the source root        c   code lines (non-blank, comments removed, import lines removed)
  i   imported modules                         m   [[constant id, count], ...] names resolved to the declaration table
  q   [qualified-name occurrences, resolved exactly to the table]
  o   name uses that resolve to the project's own declarations
  d   declarations [kind, full name]; kind "aux" marks `where` auxiliaries, structure fields and constructors
      (names only, not counted as declarations)

Name lookup follows Lean's order, line by line: local variables visible on that line first (binders of the
declaration, `variable`s, and `fun`/`∀`/`∃` binders and tactic-introduced names from the line they appear on), then
the declaration itself (for its `where` auxiliaries), the current namespace and its parents (innermost first), the
name as written, aliases, and namespaces opened by `open` (with `open … in`, `( … )`, `hiding`, `renaming` and
`scoped`, each ending with its command or section). For each candidate, a project declaration wins over a table
entry. An unresolved `A.b.c` is retried as `A.b` (Lean reads `.c` as a field access); such matches count as uses
but not as exact for the coverage figure. A first pass collects the project's declaration names. The table covers
Mathlib, Lean core and the libraries Mathlib uses; the card separates them by module.

Qualified name = Capitalized.head.name (two or more parts, head longer than one character), the names a text
scan can trace without a build.

Usage: python3 extract.py --root SRC --out OUTDIR [--procs 5] [--include LIB ...]
(anatomy.py in the repository root runs all passes in one command.)
"""
import argparse, collections, gzip, json, os, re, sys, time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mathlib_data as D

ID = r"(?:[^\W\d]|_)[\w'!?]*"                                   # one name component
CHARLIT = r"""(?<![\w'])'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|[^\n])|[^\\'\n])'"""
SCAN = re.compile(r"""/-|--|(?<![\w'])r(\#*)"|"|""" + CHARLIT)
SCAN_B = re.compile(r"""/-|--|(?<![\w'])r(\#*)"|"|""" + CHARLIT + r"|[{}]")       # inside an interpolation
STR = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
IDCHAR = re.compile(r"[\w'!?]")
IMPORT_LINE = re.compile(r"(?m)^[ \t]*(?:(?:public|private|meta)[ \t]+)*import[ \t].*$")
IMPORT_HEAD = re.compile(r"^[ \t]*(?:(?:public|private|meta)[ \t]+)*import[ \t]+(?:all[ \t]+)?")
IDENT = re.compile(r"(?<![\w.'!?])((?:[^\W\d]|_)[\w'!?]*(?:\.(?:[^\W\d]|_)[\w'!?]*)*)")
PREFIX = r"(?:(?:open[ \t](?:(?![ \t]in[ \t])[^\n])*?[ \t]in|set_option[ \t]+\S+[ \t]+\S+[ \t]+in)[ \t]+)*"   # `open X in` / `set_option o v in` before a command
DECL = re.compile(r"^([ \t]*)" + PREFIX + r"(?:@\[[^\]]*\][ \t]*)*(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|public|scoped|local|meta)[ \t]+)*"
                  r"(theorem|lemma|def|abbrev|structure|class|inductive|instance|axiom|opaque|example)\b[ \t]*((?:«[^»\n]*»|\.\{[^}\n]*\}|[^\s:({\[⦃«])*)")
CONT = re.compile(r"^[ \t]*(?:termination_by|decreasing_by|deriving|where|\||with\b)")
SKIP_LINE = re.compile(r"[ \t]*(?:open|namespace|end|section|noncomputable section|universe|variable|set_option)\b")
IN_REST = re.compile(r"^[ \t]*(?:open|set_option)\b.*?[ \t]in[ \t]+(\S.*)$")       # the command after `… in`
KEYWORDS = set("""theorem lemma def abbrev structure class inductive instance axiom opaque example fun by have show from
at with match if then else let in do where open namespace end section variable variables universe extends deriving
private protected noncomputable unsafe partial nonrec public scoped local meta import attribute mutual set_option
Type Sort Prop true false this hiding renaming""".split())
NOT_BINDERS = {"rfl", "_"}                                       # rintro/rcases pattern tokens that bind nothing


# ---------------------------------------------------------------------------------------------------- lexical cleaning
def _block_end(text, j):
    """Index just after the block comment whose opening `/-` ends at j (nesting respected)."""
    depth, n = 1, len(text)
    while depth and j < n:
        a, b = text.find("/-", j), text.find("-/", j)
        if b < 0: return n
        if 0 <= a < b: depth += 1; j = a + 2
        else: depth -= 1; j = b + 2
    return j


def _strip(text, i, brace):
    """Clean text from index i. With brace=True (the code inside an interpolation) stop at the `}` that closes it.
    Returns (cleaned text, index after the stop)."""
    out, n, depth = [], len(text), 0
    rx = SCAN_B if brace else SCAN
    while i < n:
        m = rx.search(text, i)
        if not m:
            out.append(text[i:]); i = n; break
        s = m.start(); out.append(text[i:s]); tok = m.group()
        if tok == "{":
            depth += 1; out.append(tok); i = s + 1
        elif tok == "}":
            if depth == 0: return "".join(out), s + 1
            depth -= 1; out.append(tok); i = s + 1
        elif tok == "--":
            j = text.find("\n", s); i = n if j < 0 else j
        elif tok == "/-":
            j = _block_end(text, s + 2); nl = text.count("\n", s, j)
            out.append("\n" * nl if nl else " "); i = j
        elif tok == '"':
            interp = s >= 2 and text[s - 1] == "!" and IDCHAR.match(text[s - 2]) is not None
            if not interp:
                mm = STR.match(text, s); j = mm.end() if mm else n
                out.append('""' + "\n" * text.count("\n", s, j)); i = j
            else:                                                           # s!"…{code}…": keep code in place
                out.append('""'); j = s + 1
                while j < n:
                    c = text[j]
                    if c == "\\":
                        out.append("\n" * text.count("\n", j, j + 2)); j += 2; continue
                    if c == '"': j += 1; break
                    if c == "\n": out.append("\n")
                    elif c == "{":
                        code, j = _strip(text, j + 1, True); out.append(" " + code + " "); continue
                    j += 1
                i = j
        elif tok.startswith("r"):                                           # raw string r"…", r#"…"#
            close = '"' + m.group(1); e = text.find(close, m.end()); j = n if e < 0 else e + len(close)
            out.append('""' + "\n" * text.count("\n", s, j)); i = j
        else:                                                               # character literal
            out.append("' '"); i = m.end()
    return "".join(out), i


def strip_comments(text):
    """Code with comments removed and string and character literals emptied. Every line break stays where it was
    (also inside comments, strings and interpolations), a removed comment leaves a space, and the code inside {…}
    of interpolated strings (s!"…{e}…", m!"…", f!"…") is kept."""
    return _strip(text, 0, False)[0]


def imports_of(code):
    return [t for l in IMPORT_LINE.findall(code) for t in IMPORT_HEAD.sub("", l).split()]


def X_lines(raw):
    return IMPORT_LINE.sub("", strip_comments(raw)).split("\n")


# ---------------------------------------------------------------------------------------------------- the table
NAMES = None
OWN = frozenset()


def load_index():
    """Mathlib's published declaration table: {names: name -> constant id, cname, cmod (constant -> module id), mods, ckind}."""
    global NAMES
    t = D.load_index()
    NAMES = t["names"]
    return t


def _init_worker(own):
    """Pool initializer: workers started with 'spawn' (macOS, Windows) do not inherit the parent's globals."""
    global OWN
    if NAMES is None: load_index()
    OWN = own


# ---------------------------------------------------------------------------------------------------- scopes
def parse_open(body):
    """The text after `open` (before any `in`) -> (opened [(namespace, hidden names)], {alias: full name})."""
    body = body.strip()
    if len(body) > MAX_NAME_CHARS: raise TooComplex("open command too long")
    if body.startswith("scoped ") or body.startswith("private "): return [], {}
    opens, alias = [], {}
    if " renaming " in body:
        a, b = body.split(" renaming ", 1)
        for pair in b.split(","):
            pp = re.split(r"→|->", pair)
            if len(pp) == 2 and a.split(): alias[pp[1].strip()] = a.split()[-1] + "." + pp[0].strip()
        return opens, alias
    if " hiding " in body:
        a, b = body.split(" hiding ", 1); a = a.split()
        opens += [(x, frozenset()) for x in a[:-1]]
        if a: opens.append((a[-1], frozenset(b.split())))
        return opens, alias
    for m in re.finditer(r"([^\s()]+)\s*\(([^)]*)\)|([^\s()]+)", body):
        if m.group(1):
            alias.update({x: m.group(1) + "." + x for x in m.group(2).split()})
        elif m.group(3) not in KEYWORDS:
            opens.append((m.group(3), frozenset()))
    return opens, alias


def _new_scope(ns=None):
    return {"ns": ns, "opens": [], "alias": {}, "vars": set()}


MAX_FILE_BYTES = 20 * 2**20     # a .lean file larger than this is skipped (the largest real ones are well under 1 MB)


class TooComplex(Exception):
    """A file whose scopes would need more memory than any real Lean file (e.g. thousands of nested namespaces)."""


CTX_BUDGET = 10_000_000          # per file: characters of namespace chains plus open/alias/variable entries, summed
MAX_NAME_CHARS = 4096           # a namespace, declaration name or whole `open` command longer than this: file skipped
MAX_ID_PARTS = 64                # components of one dotted name (real names use fewer than 10)
MAX_NS_PARTS = 256               # components of the current namespace (real projects use fewer than 15)
FILE_SECONDS = 120               # per file; the slowest real files take well under a second


def _out_of_time(signum, frame):
    raise TooComplex("time limit")


def limited(fn, *args):
    """fn(*args) with a per-file time limit where the platform has SIGALRM (Linux, macOS), as a backstop against inputs
    that are slow in ways no other limit catches."""
    import signal, threading
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        return fn(*args)
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:            # an enclosing limit is already running: keep its deadline
        return fn(*args)
    old = signal.signal(signal.SIGALRM, _out_of_time); signal.alarm(FILE_SECONDS)
    try:
        return fn(*args)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old)


def walk(lines):
    """One pass over a file's cleaned lines. Returns decls [(line, indent, kind, full name)], aux names, the names of
    declarations that have `where` auxiliaries, and per line the index of its scope context in ctxs. A context is
    (namespace chain innermost first, opens [(namespace, hidden)], aliases, section variables)."""
    stack = [_new_scope()]
    decls, aux, owners, ctxs, ctx_ids, ctx_of = [], [], set(), [], {}, []
    spans = []                                                     # (line, start, end) of field/helper header names
    once_pending, once_active, once_line = [], None, -1           # `open … in`: waiting (nested ones compose) / applying
    skip_lines, in_variable = set(), False                         # continuation lines of a multi-line `variable`
    where_owner, where_indent = None, None                         # (full, indent, kind) inside its where block
    ind_owner = None                                               # inductive whose constructors follow

    def compose(os_):
        opens, alias = [], {}
        for o, a_ in os_: opens += o; alias.update(a_)
        return (opens, alias)

    budget, chains = [CTX_BUDGET], {}

    def charged(names):                            # field, helper and constructor names count like declaration names
        for x in names:
            if len(x) > MAX_NAME_CHARS: raise TooComplex("name too long")
            budget[0] -= len(x)
        if budget[0] < 0: raise TooComplex("names over budget")
        return names

    def charge(o):                                 # the names an `open` creates count against the file's budget
        budget[0] -= len(o[0]) + sum(len(k) + len(v) for k, v in o[1].items())
        if budget[0] < 0: raise TooComplex("open commands over budget")

    def context():
        if len(stack) > 1000: raise TooComplex("more than 1000 nested scopes")
        names = tuple(s["ns"] for s in stack if s["ns"])
        if sum(map(len, names)) > MAX_NAME_CHARS: raise TooComplex("namespace too long")
        chain = chains.get(names)
        if chain is None:
            parts = ".".join(names).split(".") if names else []
            if len(parts) > MAX_NS_PARTS: raise TooComplex("namespace with too many components")
            cost, run = 0, -1
            for p_ in parts: run += len(p_) + 1; cost += run       # characters of all prefixes, before building them
            if cost > budget[0]: raise TooComplex("namespace prefixes over budget")
            chain = chains[names] = tuple(".".join(parts[:k]) for k in range(len(parts), 0, -1))
        opens, alias, vars_ = [], {}, set()
        for s in stack:
            opens += s["opens"]; alias.update(s["alias"]); vars_ |= s["vars"]
        if once_active:
            opens += once_active[0]; alias.update(once_active[1])
        key = (chain, tuple(opens), tuple(sorted(alias.items())), frozenset(vars_))
        if key not in ctx_ids:
            budget[0] -= sum(map(len, chain)) + len(opens) + len(alias) + len(vars_) + 1
            if budget[0] < 0: raise TooComplex("scope contexts over budget")
            ctx_ids[key] = len(ctxs); ctxs.append((chain, tuple(opens), dict(alias), frozenset(vars_)))
        return ctx_ids[key]

    cid = context()
    for li, l in enumerate(lines):
        s = l.strip()
        if not s:
            ctx_of.append(cid); continue
        ind = len(l) - len(l.lstrip())
        changed = False
        top_cmd = ind == 0 and not CONT.match(l)                   # a new command starts at column 0
        prefix_only = re.fullmatch(r"(?:@\[[^\]]*\][ \t]*)+|(?:open|set_option)\b.*[ \t]in", s) is not None
        if in_variable is not False and (ind <= in_variable or DECL.match(l)): in_variable = False
        if in_variable is not False: skip_lines.add(li); stack[-1]["vars"] |= binder_group_names(s); changed = True
        if once_active and top_cmd and li > once_line:              # `open … in` ends with its command
            once_active = None; changed = True
        if once_pending and not prefix_only:                        # the command the pending opens apply to
            once_active, once_pending, once_line = compose(once_pending), [], li; changed = True
        if ind_owner and (DECL.match(l) or (top_cmd and not s.startswith("|"))): ind_owner = None
        if where_owner and ind <= where_owner[1]: where_owner = None
        if where_owner and not DECL.match(l):
            if where_indent is None: where_indent = ind
            if ind == where_indent:                                  # a field or helper header, not a continuation
                mw = re.match(rf"[ \t]+(?:@\[[^\]]*\][ \t]*)*(?:(?:private|protected|noncomputable)[ \t]+)*((?:{ID}[ \t]+)*{ID})[ \t]*(?=[:({{\[⦃]|:=)", l)
                if mw and mw.group(1).split()[0] not in KEYWORDS:
                    nm = mw.group(1).split() if where_owner[2] in ("structure", "class") else mw.group(1).split()[:1]
                    aux += charged([where_owner[0] + "." + x for x in nm if x not in KEYWORDS])
                    spans.append((li, mw.start(1), mw.end(1)))
                    if where_owner[2] not in ("structure", "class"): owners.add(where_owner[0])
        if ind_owner and s.startswith("|"):                          # constructors
            mc = re.match(rf"\|[ \t]*({ID})", s)
            if mc: aux += charged([ind_owner + "." + mc.group(1)])
        if s.startswith("namespace "):
            stack.append(_new_scope(s.split()[1])); changed = True
        elif s == "mutual" or s.startswith("section") or s.startswith("noncomputable section"):
            stack.append(_new_scope()); changed = True
        elif s == "end" or s.startswith("end "):
            if len(stack) > 1: stack.pop()
            changed = True
        elif re.match(r"variables?\b", s):
            stack[-1]["vars"] |= binder_group_names(s); changed = True; in_variable = ind    # its indentation
        m_open = re.match(r"(?:@\[[^\]]*\][ \t]*)*open[ \t]+(.*)$", s)
        if m_open:
            body = m_open.group(1)
            mi = re.search(r"[ \t]in(?:[ \t]+(\S.*))?$", body)
            if mi:
                o = parse_open(body[:mi.start()]); charge(o)
                if mi.group(1): once_active, once_line = compose(once_pending + [o]), li; once_pending = []   # same line
                else: once_pending.append(o)
            else:
                o = parse_open(body); charge(o)
                stack[-1]["opens"] += o[0]; stack[-1]["alias"].update(o[1])
            changed = True
        m = DECL.match(l)
        if m:
            nm = re.sub(r"\.\{[^}]*\}$", "", m.group(3)).replace("«", "").replace("»", "")
            ns = ".".join(x["ns"] for x in stack if x["ns"])
            full = nm[7:] if nm.startswith("_root_.") else (ns + "." + nm if ns and nm else nm)
            if len(full) > MAX_NAME_CHARS: raise TooComplex("declaration name too long")
            budget[0] -= len(full)
            if budget[0] < 0: raise TooComplex("declaration names over budget")
            decls.append((li, len(m.group(1)), m.group(2), full))
            where_owner, where_indent = None, None
            if m.group(2) == "inductive" and full:
                ind_owner = full
                aux += charged([full + "." + c for c in re.findall(rf"\|[ \t]*({ID})", l[m.end():])])      # inductive T where | a | b
            if re.search(r"(?:^|[ \t])where[ \t]*$", s) and full and m.group(2) not in ("instance", "inductive"):
                where_owner = (full, len(m.group(1)), m.group(2))
        elif where_owner is None and decls and re.match(r"where[ \t]*$", s):
            d = decls[-1]
            if d[3] and d[2] not in ("instance", "inductive"): where_owner, where_indent = (d[3], d[1], d[2]), None
        if changed: cid = context()
        ctx_of.append(cid)
    return decls, aux, owners, ctxs, ctx_of, spans, skip_lines


# ---------------------------------------------------------------------------------------------------- local variables
BINDER_GROUP = re.compile(rf"[(\{{⦃\[][ \t]*((?:{ID}[ \t]+)*{ID})[ \t]*:(?!=)")
BINDKW = re.compile(r"(?<![\w'])(?:fun|λ)(?![\w'])|[∀∃∑∏⋃⋂⨆⨅]!?")
NAME = re.compile(ID)
TACTIC_BIND = re.compile(rf"^[ \t]*(?:·[ \t]*)?(intro|intros|rintro|ext|funext|choose!?|by_contra!?|filter_upwards[ \t]*\[[^\]]*\][ \t]*with|"
                         rf"obtain|rcases[^\n]*?with|cases'?[^\n]*?with|induction'?[^\n]*?with|by_cases|have|haveI|let|letI|set|replace)\b(.*)$")
ALT = re.compile(r"^[ \t]*\|[ \t]*([^|\n]*?)(?:=>|↦)")


def binder_group_names(text):
    return {x for g in BINDER_GROUP.findall(text) for x in g.split()}


def _cut(seg, stops):
    """seg up to the first character in stops at bracket depth 0."""
    d = 0
    for k, c in enumerate(seg):
        if c in "([{⦃⟨": d += 1
        elif c in ")]}⦄⟩": d -= 1
        elif d <= 0 and c in stops: return seg[:k]
    return seg


def strip_types(seg):
    """Binder text without type annotations: each `:` and what follows it up to the end of its bracket group (or of
    seg, at top level) is removed, so `(x : Fin (Nat.floor (exp 1)))` leaves `(x )`."""
    out, depth, skip = [], 0, None
    for c in seg:
        if c in "([{⦃⟨":
            if skip is None: out.append(c)
            depth += 1
        elif c in ")]}⦄⟩":
            depth -= 1
            if skip is not None and depth < skip: skip = None
            if skip is None: out.append(c)
        elif skip is None:
            if c == ":": skip = depth
            else: out.append(c)
    return "".join(out)


def _names(seg):
    return {x for x in NAME.findall(seg) if x not in KEYWORDS and x not in NOT_BINDERS}


def binder_segment_names(text, m):
    """Names bound by the fun/∀/∃/… keyword matched at m; the binder list may continue onto the next lines."""
    seg = _cut(text[m.end(): m.end() + 400], ",=↦" if m.group().startswith(("fun", "λ")) else ",")
    seg = re.split(r"[∈∉⊆⊂<>≤≥≠=]|(?<![\w'])in(?![\w'])", strip_types(seg))[0]
    return _names(seg)


def line_binders(line):
    """(names a line binds from that point on: tactic-introduced names,
        pattern variables of a `| … =>` alternative, visible only inside that alternative)."""
    out, alt = set(), set()
    t = TACTIC_BIND.match(line)
    if t:
        rest = re.split(r":=|(?<![\w'])using(?![\w'])|;|<;>", t.group(2))[0]
        out |= _names(strip_types(_cut(rest, ":")).replace(" with ", " "))
    a = ALT.match(line)
    if a:
        pat = strip_types(a.group(1).strip())
        toks = re.findall(r"\.?" + ID + r"(?:\." + ID + r")*", pat)
        bare = [x for x in toks if "." not in x and x not in KEYWORDS and x not in NOT_BINDERS]
        if toks and ("." in toks[0]):                              # qualified constructor (Option.some n, .inl h)
            alt = set(bare)
        elif pat.startswith(("⟨", "(", "_")) or len(bare) <= 1:
            alt = set(bare)
        else:
            alt = set(bare[1:])                                     # some n: the constructor binds nothing
    return out, alt


def _indent(l):
    return len(l) - len(l.lstrip())


def decl_locals(lines, li, e):
    """(names bound for the whole declaration by its header binders, {line: [(names, last line they are visible on)]}).
    Binders and tactic names stay visible to the end of the declaration; pattern variables of an alternative until
    the next alternative at the same indentation, or a line indented less than it."""
    block = "\n".join(lines[li:e])
    head = binder_group_names(_cut(block, ":="))
    intro = {}
    starts = [0]
    for k in range(li, e - 1): starts.append(starts[-1] + len(lines[k]) + 1)
    import bisect
    for m in BINDKW.finditer(block):                              # fun/∀/∃ binders, possibly over several lines
        names = binder_segment_names(block, m)
        if names: intro.setdefault(li + bisect.bisect_right(starts, m.start()) - 1, []).append((names, e - 1))
    for k in range(li, e):
        b, alt = line_binders(lines[k])
        if b: intro.setdefault(k, []).append((b, e - 1))
        if alt:
            ind, end = _indent(lines[k]), e - 1
            for j in range(k + 1, e):
                lj = lines[j]
                if not lj.strip(): continue
                if _indent(lj) < ind or (_indent(lj) == ind and lj.lstrip().startswith("|")):
                    end = j - 1; break
            intro.setdefault(k, []).append((alt, end))
    return head, intro


# ---------------------------------------------------------------------------------------------------- name lookup
def lookup(c):
    if c in OWN: return ("own", c, True)
    i = NAMES.get(c)
    return ("mathlib", i, True) if i is not None else None


def _resolve1(t, ctx, owner=None):
    chain, opens, alias, _ = ctx
    if owner:
        r = lookup(owner + "." + t)                              # a `where` auxiliary of the enclosing declaration
        if r: return r
    for p in chain:
        r = lookup(p + "." + t)
        if r: return r
    r = lookup(t)
    if r: return r
    h = t.split(".", 1)[0]
    if h in alias:
        r = lookup(alias[h] + t[len(h):])
        if r: return r
    for o, hidden in opens:
        if h in hidden: continue
        r = lookup(o + "." + t)
        if r: return r
    return None


def resolve_ctx(tok, ctx, locals_=frozenset(), owner=None):
    """('own', full name, exact) | ('mathlib', constant id, exact) | None, in Lean's order (see the module docstring).
    exact is False for matches found by reading trailing components as field accesses."""
    if tok.count(".") >= MAX_ID_PARTS: return None              # no real name has this many parts (keeps the fallback linear)
    if tok.startswith("_root_."):
        return lookup(tok[7:])
    head = tok.split(".", 1)[0]
    if head in locals_ or head in ctx[3]: return None          # a local variable, or a field of one
    r = _resolve1(tok, ctx, owner)
    if r: return r
    t = tok
    while "." in t:
        t = t.rsplit(".", 1)[0]                                 # A.b.c -> A.b with .c read as a field access
        r = _resolve1(t, ctx, owner)
        if r and ("." in t or t[:1].islower() or r[0] == "own"):   # not a bare library type or namespace (Real.oldName)
            return (r[0], r[1], False)
        if "." not in t: return None
    return None


def resolve(tok, prefixes):
    """Back-compatible helper: Mathlib constant id for tok under the given prefixes, or None."""
    r = resolve_ctx(tok, (tuple(prefixes), (), {}, frozenset()))
    return r[1] if r and r[0] == "mathlib" else None


def body_end(lines, li, ind):
    for lj in range(li + 1, len(lines)):
        l = lines[lj]
        if not l.strip(): continue
        if DECL.match(l): return lj                             # a new command, whatever its indentation
        if (len(l) - len(l.lstrip())) <= ind and not CONT.match(l): return lj
    return len(lines)


WORD = re.compile(r"«[^»\n]*»|[^\W\d][\w'!?.]*")


def statement_of(text, kind):
    """A declaration's statement: definitions whole; theorems up to the first `:=` at bracket depth 0 that does not
    belong to a `let`/`have` inside the type, or up to a `| … =>` alternative or `where` that starts the proof."""
    if kind in ("def", "abbrev", "structure", "class", "inductive", "instance"): return text
    d, k, n, pending, in_match = 0, 0, len(text), 0, False
    while k < n:
        c = text[k]
        if c == "«":
            e = text.find("»", k); k = n if e < 0 else e + 1; continue
        if c in "([{⦃⟨": d += 1
        elif c in ")]}⦄⟩": d -= 1
        elif d <= 0:
            if c.isalpha() or c == "_":
                w = WORD.match(text, k).group()
                if k == 0 or not IDCHAR.match(text[k - 1]):
                    if w in ("let", "have", "haveI", "letI"): pending += 1
                    elif w == "match": in_match = True
                    elif w == "where": return text[:k]
                k += len(w); continue
            if text.startswith(":=", k):
                if pending: pending -= 1
                else: return text[:k]
                k += 2; continue
            if c == "|" and not in_match and text[:k].split("\n")[-1].strip() == "":
                if re.match(r"\|[^|\n]*(?:=>|↦)", text[k:]): return text[:k]
        k += 1
    return text


def file_model(raw):
    """Everything later passes need from one file: cleaned lines, imports, declarations with body ends, aux names,
    scope contexts per line, and each declaration's local variables."""
    code = strip_comments(raw)
    imps = imports_of(code)
    lines = IMPORT_LINE.sub("", code).split("\n")
    decls, aux, owners, ctxs, ctx_of, spans, skips = walk(lines)
    ends = [body_end(lines, li, ind) for li, ind, _, _ in decls]
    locs = [decl_locals(lines, li, e) for (li, _, _, _), e in zip(decls, ends)]
    return {"lines": lines, "imports": imps, "decls": decls, "ends": ends, "aux": aux, "owners": owners,
            "ctxs": ctxs, "ctx_of": ctx_of, "locals": locs, "spans": spans, "skips": skips}


def locals_until(model, k, line):
    """Local names of declaration k visible on a line (header binders plus names bound before it and still in scope)."""
    head, intro = model["locals"][k]
    out = set(head)
    for li, entries in intro.items():
        for names, end in entries:
            if li <= line <= end: out |= names
    return frozenset(out)


# ---------------------------------------------------------------------------------------------------- passes
def scan(job):
    try:
        return limited(_scan, job)
    except (TooComplex, RecursionError, MemoryError):
        return None


def _scan(job):
    root, path = job
    try:
        raw = open(os.path.join(root, path), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    F = file_model(raw)                                           # too complex or slow: scan() skips the file
    lines, decls, ctxs, ctx_of = F["lines"], F["decls"], F["ctxs"], F["ctx_of"]
    body = lines[:]
    for li, _, _, _ in decls:                                     # a declaration's own name in its header is not a use
        m = DECL.match(body[li])
        if m and m.group(3): body[li] = body[li][:m.start(3)] + " " * (m.end(3) - m.start(3)) + body[li][m.end(3):]
    for li, a, b in F["spans"]:                                   # field and helper names where they are declared
        body[li] = body[li][:a] + " " * (b - a) + body[li][b:]
    owner = [-1] * len(lines)                                     # which declaration each line belongs to
    for k, ((li, _, _, _), e) in enumerate(zip(decls, F["ends"])):
        for x in range(li, e): owner[x] = k
    consts = collections.Counter(); nq = nqm = own = 0
    cache, locs, locs_id, cur_k, active = {}, frozenset(), 0, -2, []
    for li, l in enumerate(body):
        k = owner[li]
        changed = False
        if k != cur_k:                                            # entering a declaration (or the space between)
            cur_k, active, changed = k, [], True
        if active and any(end < li for _, end in active):
            active = [(n, end) for n, end in active if end >= li]; changed = True
        if k >= 0 and li in F["locals"][k][1]:
            active = active + F["locals"][k][1][li]; changed = True
        if changed:
            locs = frozenset(F["locals"][k][0]).union(*[n for n, _ in active]) if k >= 0 else frozenset(); locs_id += 1
        if not l.strip() or li in F["skips"]: continue
        if SKIP_LINE.match(l):
            mr = IN_REST.match(l)                                 # `open X in theorem …`: the command still counts
            if not mr: continue
            l = mr.group(1)
        ctx = ctxs[ctx_of[li]]
        own_name = decls[k][3] if k >= 0 and decls[k][3] in F["owners"] else None
        for tok in IDENT.findall(l):
            if tok in KEYWORDS or len(tok) < 2: continue
            key = (tok, ctx_of[li], locs_id)
            r = cache.get(key, 0)
            if r == 0: r = cache[key] = resolve_ctx(tok, ctx, locs, own_name)
            if r:
                if r[0] == "mathlib": consts[r[1]] += 1
                else: own += 1
            parts = tok.split(".")
            if len(parts) > 1 and parts[0][:1].isupper() and len(parts[0]) > 1:
                nq += 1
                if r and r[0] == "mathlib" and r[2]: nqm += 1
    return {"p": path, "c": sum(1 for l in lines if l.strip()), "i": F["imports"], "m": sorted(consts.items()),
            "q": [nq, nqm], "o": own, "d": [[k, f] for _, _, k, f in decls] + [["aux", a] for a in F["aux"]]}


def decl_names(job):
    root, path = job
    try:
        raw = open(os.path.join(root, path), encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    try:
        decls, aux, _, _, _, _, _ = limited(lambda: walk(X_lines(raw)))
    except (TooComplex, RecursionError, MemoryError):             # e.g. thousands of nested s!"{…}" strings
        return []
    return [f for _, _, _, f in decls if f] + aux


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--procs", type=int, default=5)
    ap.add_argument("--include", action="extend", nargs="*", default=[], help="only these top-level folders/files (e.g. the lakefile's lean_lib names)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    load_index()
    files, big = [], []
    for d, dirs, fs in os.walk(a.root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in (".lake", "lake-packages", "build")]
        for f in fs:
            p = os.path.join(d, f)
            if not f.endswith(".lean") or os.path.islink(p) or not os.path.isfile(p): continue   # links are not followed
            if os.path.getsize(p) > MAX_FILE_BYTES: big.append(os.path.relpath(p, a.root)); continue
            files.append(os.path.relpath(p, a.root).replace(os.sep, "/"))
    if a.include:
        inc = [x.rstrip("/") for x in a.include]
        files = [f for f in files if any(f == x or f == x + ".lean" or f.startswith(x + "/") for x in inc)]
    files.sort()
    global OWN
    with Pool(a.procs) as pool:                                   # first pass: the project's declaration names
        OWN = frozenset(f for fs in pool.imap_unordered(decl_names, [(a.root, f) for f in files], chunksize=64) for f in fs)
    print(f"project declarations: {len(OWN)} names, {time.time() - t0:.0f}s", flush=True)
    n = 0
    with gzip.open(os.path.join(a.out, "files.jsonl.gz"), "wt", encoding="utf-8") as out, \
            Pool(a.procs, initializer=_init_worker, initargs=(OWN,)) as pool:
        for rec in pool.imap(scan, [(a.root, f) for f in files], chunksize=32):       # in file order: the same card every run
            if rec is None: continue
            out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"); n += 1
    print(f"extract: {n} files in {time.time() - t0:.0f}s", flush=True)
    if big: print(f"skipped {len(big)} file(s) over {MAX_FILE_BYTES // 2**20} MB: {', '.join(big[:5])}", flush=True)
    if n < len(files): print(f"skipped {len(files) - n} file(s) that could not be read or were too deeply nested", flush=True)


if __name__ == "__main__":
    main()
