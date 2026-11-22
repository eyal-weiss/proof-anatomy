#!/usr/bin/env python3
"""Proof Anatomy, pass 2: the card. Counts only; no build, no LLM, no judgment calls about the mathematics.

Computes from extract.py's records:
  numbers      Lean files, code lines, theorems and lemmas, definitions
  size         the same counts against all of Mathlib (Mathlib's published declaration table)
  statement    each main theorem's statement, its names traced to Mathlib or to the project (two levels)
  areas        code lines split among Mathlib's own top-level folders, by the Mathlib names each file uses
  borrowed     share of traceable qualified names that point to Mathlib vs to the project's own declarations
  famous       theorems on Mathlib's lists (docs/1000.yaml, docs/100.yaml) whose Mathlib declaration the code names
  folder_map   the author's folders and the imports between them (drawn as a ring on the detail page)
  history      commits per month and contributor count from git history, when a repository is given
Writes OUT/card.json, OUT/card.html (card + details), OUT/card_png.html and OUT/card.png (card only).

Usage: python3 card.py --data OUT --root SRC --title "..." [--main Main.lean] [--decl NAME ...]
                       [--project-dir DIR] [--git DIR] [--url URL] [--commit SHA]
       python3 card.py --render OUT        (re-render card.html / card.png from OUT/card.json)
"""
import argparse, array, collections, datetime, gzip, html, json, math, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract as X
import mathlib_data as D

OUTSIDE = "Lean core & other libraries"


def records(path, fields=None):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            yield r if fields is None else {k: r[k] for k in fields}


def area1(mod):
    p = mod.split("/")
    return p[1] if p[0] == "Mathlib" and len(p) > 1 else OUTSIDE


GLOSS = {"Data": "Data (numbers, sets, matrices)", "Basic": "Basic (foundational facts)", "Order": "Order (orderings, lattices)",
         "Tactic": "Tactic (proof automation)", "Logic": "Logic (foundations)", "Control": "Control (programming structures)"}


def words(name):
    if name == OUTSIDE: return "Lean core (logic, numbers, lists)"
    if name in GLOSS: return GLOSS[name]
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return s[:1] + s[1:].lower()


def folder_words(name):
    """Author's folder names: split CamelCase into words, keep the author's capitalization."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


# Reading history must not let the repository's own git config start programs: transports are switched off (so a
# "promisor remote" cannot make git fetch missing objects through a configured ssh command), as are hooks, fsmonitor
# and signature checks.
GIT_OFFLINE_ENV = {"GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
GIT_OFFLINE = ["-c", "protocol.allow=never", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "log.showSignature=false"]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_history(out):
    """git log lines "date<TAB>name<TAB>email" -> [(date, name, email)]; malformed lines are dropped. Lines are split
    on "\n" only: a carriage return inside an author field must not start a fake row."""
    rows = []
    for l in out.split("\n"):
        r = l.split("\t")
        if len(r) >= 3 and DATE.fullmatch(r[0]):
            try:
                datetime.date.fromisoformat(r[0])                    # a real calendar date, e.g. not 2020-99-01
            except ValueError:
                continue
            rows.append((r[0], "\t".join(r[1:-1]), r[-1]))
    return rows


HISTORY_BYTES = 64 * 2**20        # more git log output than this: no history chart


def git_history(d):
    try:
        p = subprocess.Popen(["git", *GIT_OFFLINE, "-C", d, "log", "--no-show-signature", "--max-count=1000000",
                              "--format=%ad%x09%an%x09%ae", "--date=short"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env={**os.environ, **GIT_OFFLINE_ENV})
        import threading
        watchdog = threading.Timer(300, p.kill); watchdog.start()   # at most 5 minutes
        try:
            data = p.stdout.read(HISTORY_BYTES + 1)                   # bytes: no \r -> \n translation
        finally:
            watchdog.cancel(); p.kill(); p.wait()
    except Exception:
        return None
    if len(data) > HISTORY_BYTES: return None
    out = data.decode("utf-8", "replace")
    rows = parse_history(out)
    bot = re.compile(r"\[bot\]|^(?:github-actions|dependabot|mathlib-bors|mathlib4-bot|leanprover-community-bot|renovate)\b|"
                     r"(?:^|[-_.+])bot@|^noreply@github\.com$", re.I)
    rows = [r for r in rows if not (bot.search(r[1]) or bot.search(r[2]))]
    if len(rows) < 20: return None
    months = collections.Counter(r[0][:7] for r in rows)
    first, last = min(r[0] for r in rows), max(r[0] for r in rows)
    y0, m0 = map(int, first[:7].split("-")); y1, m1 = map(int, last[:7].split("-"))
    series, y, m = [], y0, m0
    if (y1 - y0) * 12 + m1 - m0 > 1200: return None                 # over a century: not a real history
    while (y, m) <= (y1, m1):
        k = f"{y:04d}-{m:02d}"; series.append([k, months.get(k, 0)])
        m += 1
        if m == 13: y, m = y + 1, 1
    if len(series) < 3: return None
    return {"commits": len(rows), "contributors": len({r[2].lower() for r in rows}), "first": first, "last": last,
            "months": len(series), "per_month": series,
            "rule": "commits by authors whose name or e-mail marks a bot are left out; shown only with at least 20 commits over 3 months"}


def read_project_file(project, name, limit=1_000_000):
    """A small metadata file of the project, or None. Symbolic links, non-regular files and anything outside the
    project are refused, and at most `limit` bytes are read, so a planted link cannot copy a private file (or an
    endless device) into the card."""
    p = os.path.join(project, name)
    try:
        real, base = os.path.realpath(p), os.path.realpath(project)
        if os.path.islink(p) or not os.path.isfile(p) or not real.startswith(base + os.sep): return None
        with open(p, encoding="utf-8", errors="replace") as f: return f.read(limit)
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--root", required=True)
    ap.add_argument("--title", required=True); ap.add_argument("--main", default="", help="file holding the main theorem, relative to --root")
    ap.add_argument("--decl", action="extend", nargs="*", default=[]); ap.add_argument("--project-dir")
    ap.add_argument("--git"); ap.add_argument("--url", default="")
    ap.add_argument("--short", help="short name for comparisons (defaults to the title)")
    ap.add_argument("--commit", default="", help="commit the card was made from (recorded in card.json)")
    ap.add_argument("--no-png", action="store_true")
    a = ap.parse_args()
    T = X.load_index()
    cname, cmod, mods, ckind = T["cname"], T["cmod"], T["mods"], T["ckind"]
    fpath = os.path.join(a.data, "files.jsonl.gz")
    K = {"title": a.title, "short": a.short or a.title.split(" (")[0], "url": a.url,
         "source": {"url": a.url, "commit": a.commit or None}, "tool": "proof-anatomy",
         "mathlib_table": D.fetched().get("declarations")}

    # ---------------------------------------------------------------- pass 1
    P, C, IMP, M = [], [], [], []
    kinds = collections.Counter(); proj_full = {}; proj_kind = {}
    nq = nqm = 0
    for fi, r in enumerate(records(fpath, ("p", "c", "i", "m", "q", "d"))):
        P.append(r["p"]); C.append(r["c"]); IMP.append(r["i"])
        arr = array.array("i")
        for cid, cnt in r["m"]: arr.append(cid); arr.append(cnt)
        M.append(arr); nq += r["q"][0]; nqm += r["q"][1]
        for k, f in r["d"]:
            kinds[k] += 1
            if f and f not in proj_full: proj_full[f] = fi; proj_kind[f] = k
    N, total = len(P), sum(C)
    if total == 0:
        sys.exit("no code lines found: check --root and --include (the project has no Lean code outside comments)")
    thm = kinds["theorem"] + kinds["lemma"]; dfn = kinds["def"] + kinds["abbrev"]
    K["numbers"] = {"files": N, "code_lines": total, "theorems": thm, "definitions": dfn}

    # size against Mathlib
    ml = collections.Counter(); mlmods = set()
    for c in range(len(cname)):
        m = mods[cmod[c]]
        if m.startswith("Mathlib/"): ml[ckind[c]] += 1; mlmods.add(m)
    K["mathlib"] = {"theorems": ml["theorem"], "definitions": ml["def"], "files": len(mlmods),
                    "source": "leanprover-community.github.io/mathlib4_docs declaration data"}

    # ---------------------------------------------------------------- areas (common names discounted)
    df = collections.Counter()
    for arr in M:
        for j in range(0, len(arr), 2): df[arr[j]] += 1
    nwith = sum(1 for arr in M if arr) or 1
    idf = {c: math.log((1 + nwith) / (1 + n)) + 1 for c, n in df.items()}      # smoothed: never zero
    mix = [None] * N
    mix2 = [None] * N
    for fi, arr in enumerate(M):
        if not arr: continue
        w, w2 = collections.defaultdict(float), collections.defaultdict(float)
        for j in range(0, len(arr), 2):
            c, n = arr[j], arr[j + 1]
            v = math.log1p(n) * idf[c]
            w[area1(mods[cmod[c]])] += v
            p = mods[cmod[c]].split("/")
            w2["/".join(p[1:3]) if p[0] == "Mathlib" and len(p) > 2 else area1(mods[cmod[c]])] += v
        s = sum(w.values())
        if s > 0:
            mix[fi] = {k: v / s for k, v in w.items()}; mix2[fi] = {k: v / s for k, v in w2.items()}
    modfile = {p[:-5].replace("/", "."): i for i, p in enumerate(P)}
    importers = collections.defaultdict(list); imports_of = [[] for _ in range(N)]
    for fi, imps in enumerate(IMP):
        for m in imps:
            j = modfile.get(m)
            if j is not None and j != fi: importers[j].append(fi); imports_of[fi].append(j)
    rem = [len(importers[i]) for i in range(N)]; order, q = [], [i for i in range(N) if rem[i] == 0]
    while q:
        i = q.pop(); order.append(i)
        for j in imports_of[i]:
            rem[j] -= 1
            if rem[j] == 0: q.append(j)
    reached = set(order)
    order += [i for i in range(N) if i not in reached]          # import cycles (impossible in a building project): fill last
    filled = 0
    for mx in (mix, mix2):
        glob = collections.defaultdict(float)
        for fi in range(N):
            for k, v in (mx[fi] or {}).items(): glob[k] += v * C[fi]
        gs = sum(glob.values()) or 1
        for fi in order:
            if mx[fi] is not None: continue
            src = [mx[j] for j in importers[fi] if mx[j]]
            if src:
                w = collections.defaultdict(float)
                for s_ in src:
                    for k, v in s_.items(): w[k] += v / len(src)
                mx[fi] = dict(w)
            else:
                mx[fi] = {k: v / gs for k, v in glob.items()}
            if mx is mix: filled += C[fi]
    A1, A2 = collections.defaultdict(float), collections.defaultdict(float)
    for fi in range(N):
        for k, v in (mix[fi] or {}).items(): A1[k] += v * C[fi]
        for k, v in (mix2[fi] or {}).items(): A2[k] += v * C[fi]
    K["areas"] = {k: round(100 * v / total, 2) for k, v in sorted(A1.items(), key=lambda kv: (-kv[1], kv[0]))}
    K["areas_L2"] = {k: round(100 * v / total, 3) for k, v in sorted(A2.items(), key=lambda kv: (-kv[1], kv[0]))}
    K["areas_filled_pct"] = round(100 * filled / total, 2) if total else 0

    # ---------------------------------------------------------------- borrowed vs new (all name uses, resolved on both sides)
    own = [0] * N; fq = [0] * N; fcore = [0] * N
    is_ml = [m.startswith("Mathlib/") for m in mods]
    for fi, r in enumerate(records(fpath, ("o", "m"))):
        own[fi] = r["o"]
        for cid, cnt in r["m"]:
            if is_ml[cmod[cid]]: fq[fi] += cnt
            else: fcore[fi] += cnt
    tot_own, tot_ml, tot_core = sum(own), sum(fq), sum(fcore)
    tot_all = max(1, tot_own + tot_ml + tot_core)
    # folder level: descend while one folder holds more than 80% of the code lines
    prefix, depth = "", 0
    while depth < 4:
        g = collections.Counter()
        for p, c in zip(P, C):
            if p.startswith(prefix):
                rest = p[len(prefix):].split("/")
                if len(rest) > 1: g[rest[0]] += c
        if not g: break
        topf, topc = min(g.items(), key=lambda kv: (-kv[1], kv[0]))          # ties broken by name: same card every run
        if topc > 0.8 * sum(C[i] for i, p in enumerate(P) if p.startswith(prefix)):
            prefix += topf + "/"; depth += 1
        else:
            break
    fold = collections.defaultdict(lambda: [0, 0, 0])
    for fi, p in enumerate(P):
        if not p.startswith(prefix): continue
        rest = p[len(prefix):].split("/")
        key = rest[0] if len(rest) > 1 else f"(files directly in {prefix or 'the top folder'})"
        fold[key][0] += C[fi]; fold[key][1] += own[fi]; fold[key][2] += fq[fi] + fcore[fi]
    rows = [kv for kv in sorted(fold.items(), key=lambda kv: (-kv[1][0], kv[0])) if kv[1][0] >= 0.005 * total][:10]
    K["borrowed"] = {"mathlib_pct": round(100 * tot_ml / tot_all, 1), "core_pct": round(100 * tot_core / tot_all, 1),
                     "own_pct": round(100 * tot_own / tot_all, 1),
                     "mathlib_uses": tot_ml, "core_uses": tot_core, "own_uses": tot_own,
                     "folder_level": prefix or "(top level)",
                     "by_folder": [{"folder": k, "lines_pct": round(100 * v[0] / total, 1),
                                    "own_pct": round(100 * v[1] / max(1, v[1] + v[2]), 1)} for k, v in rows]}

    # ---------------------------------------------------------------- statement provenance (same lookup as pass 1)
    X.OWN = frozenset(proj_full)
    pidx = {p: i for i, p in enumerate(P)}
    models = {}

    def texts(fi):
        if fi not in models:
            raw = open(os.path.join(a.root, P[fi]), encoding="utf-8", errors="replace").read()
            try:
                F = X.limited(X.file_model, raw)
            except (X.TooComplex, RecursionError, MemoryError):
                models[fi] = {}; return models[fi]
            out = {}
            for k, ((li, ind, kind, full), e) in enumerate(zip(F["decls"], F["ends"])):
                stmt = X.statement_of("\n".join(F["lines"][li:e]), kind)
                text = "\n".join(x.rstrip() for x in stmt.split("\n") if x.strip())
                owner = full if full in F["owners"] else None
                per_line = []                                  # each line with the scope and locals in force on it
                for j, ln in enumerate(stmt.split("\n")):
                    if j == 0:
                        hm = X.DECL.match(ln)
                        if hm and hm.group(3): ln = ln[:hm.start(3)] + " " + ln[hm.end(3):]   # its own name is not a reference
                    per_line.append((ln, F["ctxs"][F["ctx_of"][li + j]], (F, k, li + j)))   # locals: computed if traced
                out[full] = (kind, text, per_line, owner)
            models[fi] = out
        return models[fi]

    def classify(tok, ctx, locs, owner=None):
        r = X.resolve_ctx(tok, ctx, locs, owner)
        if r and r[0] == "mathlib":
            m = mods[cmod[r[1]]]
            return {"name": tok, "where": "mathlib" if m.startswith("Mathlib/") else "core", "module": m.replace("/", "."), "full": cname[r[1]],
                    "kind": ckind[r[1]]}
        if r and r[0] == "own" and r[1] in proj_full:
            return {"name": tok, "where": "project", "file": P[proj_full[r[1]]], "full": r[1], "kind": proj_kind.get(r[1])}
        head = tok.split(".")[0]
        if head in locs or head in ctx[3]:
            return {"name": tok, "where": "local"}
        if "." in tok and not head[:1].isupper():
            return {"name": tok, "where": "field"}
        return {"name": tok, "where": "unresolved"}

    def names_list(xs):
        return ", ".join(xs[:-1]) + " and " + xs[-1] if len(xs) > 1 else xs[0]

    prov, sentences = [], []
    mi = [i for i, p in enumerate(P) if p == a.main]

    def trace():
        if not mi: return
        tx = texts(mi[0])
        if a.decl: mains = [f for f in tx if f in a.decl or f.split(".")[-1] in a.decl]      # as asked, any kind
        else: mains = [f for f, v in tx.items() if v[0] in ("theorem", "lemma")][:4]
        for full in mains:
            queue, seen, items = [(full, mi[0], 0)], set(), []
            while queue and len(items) < 12:
                f, fi, dep = queue.pop(0)
                if (f, fi) in seen: continue
                seen.add((f, fi))
                t2 = texts(fi)
                if f not in t2: continue
                kind, stmt, per_line, owner = t2[f]
                refs, toks = [], set()
                for ln, ctx, lz in per_line:
                    locs = X.locals_until(*lz)
                    for tok in X.IDENT.findall(ln):
                        if tok in X.KEYWORDS or len(tok) < 2: continue
                        c = classify(tok, ctx, locs, owner)
                        key_ = (tok, c["where"])
                        if key_ in toks or (c["where"] in ("local", "unresolved") and len(tok) <= 2): continue
                        toks.add(key_); refs.append(c)
                        if c["where"] == "project" and dep < 2 and c["full"] != f: queue.append((c["full"], pidx[c["file"]], dep + 1))
                items.append({"name": f, "file": P[fi], "kind": kind, "depth": dep, "text": stmt[:1200], "refs": refs})
            prov.append(items)
            top = items[0] if items else None
            if top:
                tac = lambda r: "Tactic." in (r.get("module") or "") or r.get("kind") in ("theorem", "lemma", "axiom")   # definitions only
                ml_ = [r["name"] for r in top["refs"] if r["where"] == "mathlib" and not tac(r)]
                core_ = [r["name"] for r in top["refs"] if r["where"] == "core" and not tac(r)]
                pj = [r["name"] for r in top["refs"] if r["where"] == "project" and not tac(r)]
                untr = [r["name"] for r in top["refs"] if r["where"] == "unresolved" and r["name"][:1].isupper()]
                axioms = list(dict.fromkeys(r["name"] for r in top["refs"] if r.get("kind") == "axiom"))
                ml_, core_, pj = ([x for x in xs if x not in axioms] for xs in (ml_, core_, pj))
                below = list(dict.fromkeys(r["name"] for it in items[1:] for r in it["refs"] if r["where"] == "mathlib" and not tac(r)))
                deeper = any(r["where"] == "project" for it in items[1:] if it["depth"] == 2 for r in it["refs"])
                pl = lambda xs, one, many: one if len(xs) == 1 else many
                core_s = f" and Lean's built-in {pl(core_, 'definition', 'definitions')} {names_list(core_[:4])}" if core_ else ""
                if not pj and ml_:
                    s_ = f"{full} is stated with Mathlib's own {pl(ml_, 'definition', 'definitions')} {names_list(ml_[:6])}{core_s}."
                elif pj:
                    s_ = f"{full} is stated with the project's own {pl(pj, 'definition', 'definitions')} {names_list(pj[:4])}"
                    s_ += (f", which {pl(pj, 'is', 'are')} built on Mathlib's {names_list(below[:6])}" if below else "") + ("; some go further into project definitions." if deeper else ".")
                    extra = [x for x in ml_ if x not in below]
                    if extra: s_ += f" It also uses Mathlib's {names_list(extra[:4])}."
                elif core_:
                    s_ = f"{full} is stated with Lean's built-in {pl(core_, 'definition', 'definitions')} {names_list(core_[:6])} only, without Mathlib or project definitions."
                elif untr:
                    s_ = f"{full} is stated with {pl(untr, 'a name', 'names')} a text scan cannot trace ({names_list(untr[:4])}); a build would show where {pl(untr, 'it comes', 'they come')} from."
                    untr = []
                else:
                    s_ = f"{full} is stated with notation only (numbers, arithmetic, logic), without named definitions."
                if untr: s_ += f" Not traced without a build: {names_list(untr[:4])}."
                if axioms: s_ += f" It assumes the {pl(axioms, 'axiom', 'axioms')} {names_list(axioms[:4])}."
                sentences.append(s_)

    try:
        X.limited(trace)                           # time limit; the memory limit is set by anatomy.py (Linux)
    except (X.TooComplex, RecursionError, MemoryError):
        prov[:], sentences[:] = [], []
        print("the main theorem's statement was too complex to trace; the card leaves it out", flush=True)
    K["statement"] = {"main_file": a.main, "sentences": sentences, "trace": prov}

    # ---------------------------------------------------------------- famous theorems (Mathlib's lists)
    import yaml
    want = {}
    for fn in ("1000.yaml", "100.yaml"):
        y = yaml.safe_load(open(D.path(fn), encoding="utf-8"))
        for key, e in y.items():
            if not isinstance(e, dict): continue
            ds = e.get("decls") or ([e["decl"]] if e.get("decl") else [])
            for dname in ds if isinstance(ds, list) else [ds]:
                cid = X.NAMES.get(str(dname))
                if cid is not None:
                    want.setdefault(cid, e.get("title", str(key)))
    # one title per theorem: the 1000+ list's title is used when both lists name the same declaration
    tfiles = collections.defaultdict(set)
    for fi, arr in enumerate(M):
        for j in range(0, len(arr), 2):
            t_ = want.get(arr[j])
            if t_: tfiles[t_].add(fi)
    K["famous"] = [{"title": t_, "files": len(s_)} for t_, s_ in sorted(tfiles.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

    K["map"] = None                                             # (the import-community map is not part of the card)
    edges = [(i, j) for i in range(N) for j in imports_of[i]]

    # folder-level map: the author's folders at the first level with >= 8 folders holding >= 1% of lines each
    K["folder_map"] = None
    if N >= 50 and edges:
        lvl, best = None, None
        for d_ in (1, 2, 3):
            g = collections.Counter()
            for p, c in zip(P, C):
                parts = p.split("/")
                if len(parts) > d_: g["/".join(parts[:d_])] += c
            if sum(1 for v in g.values() if v >= 0.01 * total) >= 8:
                lvl = d_; break
            best = d_
        lvl = lvl or best or 1
        def fkey(p):
            parts = p.split("/")
            return "/".join(parts[:lvl]) if len(parts) > lvl else ("/".join(parts[:-1]) + "/" if len(parts) > 1 else "(root)")
        fk = [fkey(p) for p in P]
        fl = collections.Counter()
        for i in range(N): fl[fk[i]] += C[i]
        keepf = [k for k, v in sorted(fl.items(), key=lambda kv: (-kv[1], kv[0]))[:30] if v >= 0.003 * total]
        kf = set(keepf); WF = collections.Counter()
        for i, j in edges:
            u, v = fk[i], fk[j]
            if u != v and u in kf and v in kf: WF[tuple(sorted((u, v)))] += 1
        last = collections.Counter(k.split("/")[-1] for k in keepf)
        lab = lambda k: k.rstrip("/").split("/")[-1] if k.endswith("/") else (k.split("/")[-1] + (f" ({k.split('/')[-2]})" if last[k.split("/")[-1]] > 1 and "/" in k else ""))
        K["folder_map"] = {"level": lvl, "nodes": [{"id": k, "lines_pct": round(100 * fl[k] / total, 2), "label": lab(k), "folder": k} for k in keepf],
                           "edges": [[u, v, w] for (u, v), w in sorted(WF.items(), key=lambda kv: (-kv[1], kv[0]))[:80]], "shown": len(keepf),
                           "folders": len(fl)}

    # ---------------------------------------------------------------- history, pinned Mathlib
    K["history"] = git_history(a.git) if a.git else None
    K["pins"] = None
    if a.project_dir:
        rev = tc = None
        try:
            man = json.loads(read_project_file(a.project_dir, "lake-manifest.json") or "{}")
            rev = next((p.get("rev") for p in man.get("packages", []) if p.get("name") == "mathlib"), None)
            rev = rev if isinstance(rev, str) and re.fullmatch(r"[0-9A-Za-z._/-]{1,100}", rev) else None
        except Exception:
            pass
        tc = (read_project_file(a.project_dir, "lean-toolchain") or "").strip()
        tc = tc if re.fullmatch(r"[0-9A-Za-z._/:+-]{1,100}", tc) else None       # e.g. leanprover/lean4:v4.36.0
        if rev or tc: K["pins"] = {"mathlib_rev": rev, "toolchain": tc}
    K["coverage"] = {"qualified_uses": nq, "resolved_to_mathlib_pct": round(100 * nqm / max(1, nq), 1)}
    with open(os.path.join(a.data, "card.json"), "w", encoding="utf-8") as f:
        json.dump(K, f, indent=1, ensure_ascii=False)
    render(a.data, png=not a.no_png)


# ====================================================================== rendering
CSS = r"""
:root{--surface:#fcfcfb;--panel:#f3f3f0;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#7b7a75;--rule:#dddcd6;--bar:#2a78d6;--own:#eb6834;--core:#9fb7d3;
--font:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--surface:#1a1a19;--panel:#242422;--ink:#f0efec;--ink-2:#c3c2b7;--ink-3:#96958c;--rule:#3a3a37;--bar:#3987e5;--own:#d95926;--core:#4c6280}}
:root[data-theme="dark"]{--surface:#1a1a19;--panel:#242422;--ink:#f0efec;--ink-2:#c3c2b7;--ink-3:#96958c;--rule:#3a3a37;--bar:#3987e5;--own:#d95926;--core:#4c6280}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);font:15px/1.5 var(--font)}
main{max-width:1000px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left)) 56px max(16px,env(safe-area-inset-right))}
.card{background:var(--surface);padding:6px 0 12px}
.kicker{color:var(--ink-3);font-size:13px;margin:0}
h1{font-size:30px;line-height:1.15;margin:4px 0 4px}
h2{font-size:19px;margin:34px 0 6px;padding-top:12px;border-top:1px solid var(--rule)}
h3{font-size:15px;margin:18px 0 6px}
p{margin:6px 0 10px;max-width:74ch}.sub{color:var(--ink-2)}.note{color:var(--ink-3);font-size:13px}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 8px}
.num{background:var(--panel);border-radius:8px;padding:10px 12px}.num b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.num span{color:var(--ink-2);font-size:13px}
.size{color:var(--ink-2);margin:2px 0 12px}
.stmt{background:var(--panel);border-radius:8px;padding:10px 14px;margin:10px 0 14px;font-size:14.5px}
.bars{display:grid;grid-template-columns:minmax(120px,250px) 1fr 52px;gap:5px 10px;align-items:center;margin:8px 0}
.bars .l{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars .b{height:12px;background:var(--bar);border-radius:0 4px 4px 0;min-width:2px}
.bars .v{font-size:13px;text-align:right;color:var(--ink-2);font-variant-numeric:tabular-nums}
.stack{display:flex;height:18px;border-radius:4px;overflow:hidden;margin:6px 0 4px;gap:2px}
.stack div{height:100%}.leg{display:flex;gap:16px;font-size:13px;color:var(--ink-2);flex-wrap:wrap}
.leg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.chips span{display:inline-block;background:var(--panel);border-radius:6px;padding:3px 9px;margin:3px 4px 3px 0;font-size:13.5px}
.chips span em{font-style:normal;color:var(--ink-3);margin-left:6px;font-size:12.5px}
.foot{color:var(--ink-3);font-size:12px;margin-top:14px}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--rule);vertical-align:top}
th{color:var(--ink-2);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.trace{background:var(--panel);border-radius:8px;padding:8px 12px;margin:8px 0}.trace pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.45 var(--mono);margin:4px 0}
.m{color:var(--bar)}.pj{color:var(--own)}.u{color:var(--ink-3)}
.tchips span{display:inline-block;margin:2px 8px 2px 0;font-size:12.5px}
.cols{display:flex;align-items:flex-end;gap:1px;height:90px;border-bottom:1px solid var(--rule)}.cols div{flex:1;background:var(--bar);border-radius:2px 2px 0 0;min-height:1px}
.axis{display:flex;justify-content:space-between;color:var(--ink-3);font-size:12px}
svg text{fill:var(--ink);font:12px var(--font)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);font-size:12.5px;padding:5px 8px;border-radius:6px;opacity:0;max-width:320px;z-index:9}
@media (max-width:620px){.nums{grid-template-columns:repeat(2,1fr)}.bars{grid-template-columns:minmax(100px,140px) 1fr 46px}h1{font-size:24px}}
"""


def esc(s): return html.escape(str(s))


def fmt(n): return f"{n:,}"


def ratio(a, b):
    if not b: return "–"
    r = a / b
    if r >= 1.05: return f"{r:.1f}×"
    if r >= 0.95: return "about the same as"
    p = 100 * r
    return f"{p:.1f}%" if p >= 0.1 else "under 0.1%"


def size_line(K):
    n, m = K["numbers"], K["mathlib"]
    rt, rd = n["theorems"] / max(1, m["theorems"]), n["definitions"] / max(1, m["definitions"])
    lib = "all of Mathlib, the Lean community's shared library"
    if rt >= 1.05:
        return f"{rt:.1f} times as many theorems as {lib}, and {rd:.1f} times as many definitions."
    if rt >= 0.95:
        return f"About as many theorems as {lib}."
    p = 100 * rt
    lead = "Under 0.1%" if p < 0.1 else f"About {p:.1f}%"
    return f"{lead} of the theorem count of {lib} ({fmt(m['theorems'])} theorems)."


def bars(rows, unit="%"):
    out = ['<div class="bars">']
    mx = max([v for _, v in rows] + [1e-9])
    for k, v in rows:
        out.append(f'<div class="l" title="{esc(k)}">{esc(k)}</div><div><div class="b" style="width:{100 * v / mx:.2f}%" data-tip="{esc(k)}: {v:.1f}{unit}"></div></div><div class="v">{v:.1f}{unit}</div>')
    out.append("</div>")
    return "".join(out)


def card_block(K):
    n = K["numbers"]
    areas = [(words(k), v) for k, v in K["areas"].items()]
    big = [(k, v) for k, v in areas if v >= 1.0][:12]
    rest = [(k, v) for k, v in areas if (k, v) not in big and v >= 0.05]
    b = K["borrowed"]
    out = [f'<div class="card"><p class="kicker">What the Lean code is made of · counted from the source text, no build, no AI judgment</p>',
           f'<h1>{esc(K["title"])}</h1>',
           '<div class="nums">' + "".join(f'<div class="num"><b>{fmt(v)}</b><span>{l}</span></div>' for v, l in
                                          ((n["files"], "Lean files"), (n["code_lines"], "lines of code"), (n["theorems"], "theorems and lemmas"), (n["definitions"], "definitions"))) + "</div>",
           f'<p class="size">{esc(size_line(K))}</p>']
    if K["statement"]["sentences"]:
        out.append(f'<div class="stmt"><b>The main theorem.</b> {esc(K["statement"]["sentences"][0])}</div>')
    out.append('<h3>Which parts of Mathlib the code is built on</h3>')
    out.append(bars(big))
    if rest:
        out.append('<p class="note">Also: ' + ", ".join(f"{esc(k)} {v:.1f}%" for k, v in rest[:10]) + (" and others under 0.1%." if len(rest) > 10 else ".") + '</p>')
    core = b.get("core_pct", 0)
    out.append(f'<h3>Borrowed or new</h3><div class="stack"><div style="width:{b["mathlib_pct"]}%;background:var(--bar)" data-tip="Mathlib: {b["mathlib_pct"]}%"></div>'
               + (f'<div style="width:{core}%;background:var(--core)" data-tip="Lean core and other libraries: {core}%"></div>' if core else "") +
               f'<div style="width:{b["own_pct"]}%;background:var(--own)" data-tip="the project\'s own: {b["own_pct"]}%"></div></div>'
               f'<div class="leg"><span><i style="background:var(--bar)"></i>{b["mathlib_pct"]:.0f}% of traced names come from Mathlib</span>'
               + (f'<span><i style="background:var(--core)"></i>{core:.0f}% from Lean core and other libraries</span>' if core else "") +
               f'<span><i style="background:var(--own)"></i>{b["own_pct"]:.0f}% are the project\'s own</span></div>')
    if K["famous"]:
        out.append('<h3>Famous theorems it uses</h3><div class="chips">' +
                   "".join(f'<span>{esc(f["title"])}<em>{fmt(f["files"])} file{"s" if f["files"] != 1 else ""}</em></span>' for f in K["famous"][:8]) + "</div>")
    if K.get("resemblance"):
        r = K["resemblance"]["closest"][:2]
        out.append('<p class="sub" style="margin-top:12px"><b>Closest in its mix of Mathlib areas</b> (among ' + str(K["resemblance"]["compared"]) + ' formalizations compared): ' +
                   "; ".join(f'{esc(x.get("short") or x["title"])} ({x["similarity"]:.2f})' for x in r) + '</p>')
    if K.get("history"):
        h = K["history"]
        out.append(f'<p class="sub"><b>Built over {h["months"]} months</b> ({esc(h["first"][:7])} to {esc(h["last"][:7])}): {fmt(h["commits"])} commits by {fmt(h["contributors"])} contributors.</p>')
    out.append(f'<p class="foot">Proof Anatomy · names traced against Mathlib\'s published declaration table · {esc(K.get("url", ""))}</p></div>')
    return "".join(out)


def details(K):
    o = []
    st = K["statement"]
    o.append('<h2>The main theorem, traced</h2><p class="sub">Each name in the statement, and in the project definitions it uses (two levels): '
             '<span class="m">Mathlib or Lean core</span>, <span class="pj">the project</span>, <span class="u">local name or field (needs a build)</span>.</p>')
    for s in st["sentences"][1:]:
        o.append(f'<p>{esc(s)}</p>')
    for items in st["trace"]:
        for it in items:
            tipw = {"unresolved": "not traced: needs a build", "local": "local name", "field": "field of a local name"}
            chips = "".join(f'<span class="{"m" if r["where"] in ("mathlib", "core") else "pj" if r["where"] == "project" else "u"}" data-tip="{esc(r.get("module") or r.get("file") or tipw.get(r["where"], r["where"]))}">{esc(r["name"])}</span>' for r in it["refs"])
            o.append(f'<div class="trace"><div class="note">{"↳ " * it["depth"]}{esc(it["file"])}</div><pre>{esc(it["text"])}</pre><div class="tchips">{chips}</div></div>')
    b = K["borrowed"]
    o.append(f'<h2>Borrowed or new, by the author\'s folders</h2><p class="sub">Every name use that resolves to a declaration of Mathlib, of Lean core and the other libraries Mathlib uses, or of the project itself counts for that side ({fmt(b["mathlib_uses"])} Mathlib uses, {fmt(b.get("core_uses", 0))} Lean core and other libraries, {fmt(b["own_uses"])} own uses). "Own names" is the project\'s share. Folders under <code>{esc(b["folder_level"])}</code>.</p>')
    o.append('<table><thead><tr><th>Folder</th><th class="n">Share of code</th><th class="n">Own names</th></tr></thead><tbody>' +
             "".join(f'<tr><td>{esc(r["folder"])}</td><td class="n">{r["lines_pct"]}%</td><td class="n">{r["own_pct"]}%</td></tr>' for r in b["by_folder"]) + "</tbody></table>")
    if len(K["famous"]) > 8:
        o.append('<h2>All theorems from Mathlib\'s lists that the code uses</h2><p class="sub">Mathlib\'s <code>docs/1000.yaml</code> (the 1000+ theorems project) and <code>docs/100.yaml</code> (Freek Wiedijk\'s 100 theorems) name the Mathlib declaration proving each theorem; a file counts when it names that declaration.</p>')
        o.append('<div class="chips">' + "".join(f'<span>{esc(f["title"])}<em>{fmt(f["files"])}</em></span>' for f in K["famous"]) + "</div>")
    if K.get("resemblance"):
        r = K["resemblance"]
        o.append(f'<h2>Resemblance to other formalizations</h2><p class="sub">{esc(r["method"])}</p><table><thead><tr><th>Formalization</th><th class="n">Similarity</th></tr></thead><tbody>' +
                 "".join(f'<tr><td>{esc(x["title"])}</td><td class="n">{x["similarity"]:.2f}</td></tr>' for x in r["closest"]) + "</tbody></table>")
    if K.get("folder_map"):
        o.append(ring_svg(K["folder_map"]))
    if K.get("history"):
        h = K["history"]; mx = max(c for _, c in h["per_month"]) or 1
        o.append(f'<h2>Commits per month</h2><p class="sub">{fmt(h["commits"])} commits by {fmt(h["contributors"])} contributors from {esc(h["first"])} to {esc(h["last"])} ({esc(h["rule"])}).</p><div class="cols">' +
                 "".join(f'<div style="height:{100 * c / mx:.1f}%" data-tip="{esc(m_)}: {c} commits"></div>' for m_, c in h["per_month"]) +
                 f'</div><div class="axis"><span>{esc(h["per_month"][0][0])}</span><span>{esc(h["per_month"][-1][0])}</span></div>')
    pins = K.get("pins") or {}
    tbl = K.get("mathlib_table") or {}
    o.append('<h2>Method and limits</h2><ul class="note">'
             f'<li>Counts are from the source text: comments and import lines removed; theorems = <code>theorem</code> and <code>lemma</code> declarations; definitions = <code>def</code> and <code>abbrev</code>. Mathlib\'s counts come from its published declaration table{(" as downloaded on " + esc(tbl["fetched"])) if tbl.get("fetched") else ""} ({fmt(K["mathlib"]["theorems"])} theorems, {fmt(K["mathlib"]["definitions"])} definitions, {fmt(K["mathlib"]["files"])} files); the two are counted differently, so ratios are approximate.</li>'
             f'<li>Areas: each file\'s lines are split among Mathlib\'s top-level folders by the Mathlib names it uses, each name weighted by log(1 + uses) and discounted by how many of the project\'s files use it (1 + log((1 + files) / (1 + files using it)), where files counts the files that use any name from the table). Files with no Mathlib name ({K["areas_filled_pct"]}% of lines) take the mix of the files that import them.</li>'
             f'<li>Borrowed or new: each name is looked up in Lean\'s order, line by line (local variables, then the current namespace and its parents, then the name as written, then opened namespaces); the first match decides whether it is the project\'s own, Mathlib\'s, or from Lean core and the other libraries Mathlib uses. Local variables and names Lean generates are not counted.</li>'
             f'<li>Famous theorems: only declarations that Mathlib\'s own lists name (theorems, and the definitions or instances some entries point to), and only when the code names them; a theorem used through a tactic is missed.</li>'
             f'<li>Map: the author\'s folders at the first level with at least 8 folders holding 1% of the lines each; a curve joins two folders when files in one import files in the other.</li>'
             f'<li>Text only: no build, so no check that the proof is correct, and no exact dependency trace.' + (f' The project pins Mathlib {esc((pins.get("mathlib_rev") or "")[:10])}; the table is today\'s, so renamed names may not resolve ({K["coverage"]["resolved_to_mathlib_pct"]}% of qualified names were traced to the table).' if pins else '') + '</li></ul>')
    return "".join(o)


def ring_svg(mp):
    """Author's folders on a ring (largest first, clockwise from the top); curves through the middle show imports."""
    W, H, R = 960, 700, 230
    cx, cy = W / 2, H / 2
    nodes = sorted(mp["nodes"], key=lambda n: (-n["lines_pct"], n["folder"]))
    k = len(nodes); mxl = max(n["lines_pct"] for n in nodes) or 1
    pos, ang = {}, {}
    for i, n in enumerate(nodes):
        t = -math.pi / 2 + 2 * math.pi * i / k
        ang[n["id"]] = t; pos[n["id"]] = (cx + R * math.cos(t), cy + R * math.sin(t))
    mw = max([e[2] for e in mp["edges"]] + [1])
    o = [f'<h2>A map of how it is built</h2><p class="sub">The author\'s {mp["shown"]} largest folders (of {mp["folders"]} at this level), largest first, clockwise from the top. Circle area follows lines of code; a curve joins two folders when files in one import files in the other, thicker for more imports.</p>',
         f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Folders of the project and the imports between them">']
    for u, v, w in sorted(mp["edges"], key=lambda e: (e[2], e[0], e[1]))[-60:]:
        if u in pos and v in pos:
            (x1, y1), (x2, y2) = pos[u], pos[v]
            o.append(f'<path d="M{x1:.1f},{y1:.1f} Q{cx:.1f},{cy:.1f} {x2:.1f},{y2:.1f}" fill="none" stroke="var(--bar)" stroke-opacity="{0.12 + 0.55 * w / mw:.2f}" stroke-width="{0.6 + 4.5 * w / mw:.2f}" data-tip="{esc(u.split("/")[-1])} – {esc(v.split("/")[-1])}: {w:,} imports"/>')
    for n in nodes:
        x, y = pos[n["id"]]; r = 4 + 26 * math.sqrt(n["lines_pct"] / mxl); t = ang[n["id"]]
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="var(--bar)" stroke="var(--surface)" stroke-width="2" data-tip="{esc(n["folder"])}: {n["lines_pct"]}% of lines"/>')
        lx, ly = cx + (R + r + 10) * math.cos(t), cy + (R + r + 10) * math.sin(t)
        anchor = "start" if math.cos(t) > 0.15 else "end" if math.cos(t) < -0.15 else "middle"
        dy = 4 if anchor != "middle" else (-4 if math.sin(t) < 0 else 14)
        o.append(f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anchor}">{esc(folder_words(n["label"]))} <tspan fill="var(--ink-3)">{n["lines_pct"]:.1f}%</tspan></text>')
    o.append("</svg>")
    return "".join(o)


TIPJS = """<script>const tip=document.getElementById("tip");document.addEventListener("mousemove",e=>{const t=e.target.closest("[data-tip]");if(!t){tip.style.opacity=0;return}tip.textContent=t.dataset.tip;tip.style.opacity=1;tip.style.left=Math.min(e.clientX+12,innerWidth-330)+"px";tip.style.top=(e.clientY+14)+"px"})</script>"""


def render(out_dir, png=True):
    K = json.load(open(os.path.join(out_dir, "card.json"), encoding="utf-8"))
    head = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(K["title"])} · Proof Anatomy</title><style>{CSS}</style></head><body>'
    page = head + f'<main>{card_block(K)}{details(K)}</main><div id="tip"></div>{TIPJS}</body></html>'
    with open(os.path.join(out_dir, "card.html"), "w", encoding="utf-8") as f: f.write(page)
    pc = head.replace("<body>", '<body style="width:1080px">') + f'<main style="max-width:1080px;padding:36px 44px 28px">{card_block(K)}</main></body></html>'
    with open(os.path.join(out_dir, "card_png.html"), "w", encoding="utf-8") as f: f.write(pc)
    if png and not shot(os.path.join(out_dir, "card_png.html"), os.path.join(out_dir, "card.png")):
        old = os.path.join(out_dir, "card.png")
        if os.path.exists(old): os.remove(old)                  # never leave a picture of earlier numbers


CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]


def find_chrome():
    import shutil
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        if shutil.which(n): return shutil.which(n)
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)


def _browser_limits():
    """The browser renders only this tool's own escaped page; it reserves much address space, so the memory limit that
    anatomy.py sets for card.py and corpus.py (a soft limit, Linux) is lifted for it."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (resource.getrlimit(resource.RLIMIT_AS)[1],) * 2)
    except Exception:
        pass


def shot(src, dst):
    """card.png from card_png.html with a headless Chrome/Chromium/Edge (skipped, with a note, when none is installed)."""
    import pathlib
    ch = find_chrome()
    if not ch:
        print("card.png skipped: no Chrome, Chromium or Edge found (card.html has the same card).", flush=True); return False
    tmp = dst + ".tmp.png"
    if os.path.exists(tmp): os.remove(tmp)                         # never promote a stale screenshot
    try:
        r = subprocess.run([ch, "--headless=new", "--disable-gpu", *(["--no-sandbox"] if getattr(os, "geteuid", lambda: 1)() == 0 else []), "--hide-scrollbars", "--force-device-scale-factor=2",
                            "--window-size=1080,2600", f"--screenshot={tmp}", pathlib.Path(src).resolve().as_uri()],
                           capture_output=True, timeout=120, preexec_fn=_browser_limits if sys.platform.startswith("linux") else None)
        ok = r.returncode == 0 and os.path.exists(tmp)
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    if not ok:
        if os.path.exists(tmp): os.remove(tmp)
        print("card.png skipped: the browser did not produce a screenshot (card.html has the same card).", flush=True); return False
    try:
        from PIL import Image
        import numpy as np
    except ImportError:                                            # no Pillow: keep the uncropped screenshot
        os.replace(tmp, dst); return True
    try:
        im = Image.open(tmp).convert("RGB"); a_ = np.asarray(im.convert("L"))
        bg = a_[5, 5]; rows = np.where((np.abs(a_.astype(int) - int(bg)) > 8).any(axis=1))[0]
        h = int(rows.max()) + 50 if len(rows) else im.size[1]
        im.crop((0, 0, im.size[0], min(im.size[1], h))).save(dst)
        return True
    except Exception:
        print("card.png skipped: the screenshot could not be read.", flush=True)
        return False
    finally:
        if os.path.exists(tmp): os.remove(tmp)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render":
        render(sys.argv[2])
    else:
        main()
