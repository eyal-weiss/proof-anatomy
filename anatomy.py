#!/usr/bin/env python3
"""Proof Anatomy: a one-page card for a Lean 4 project, computed from its source text.

  python3 anatomy.py SOURCE [--main FILE] [--decl THEOREM ...] [--title "..."] [--out cards]

SOURCE is a git URL (such as https://github.com/teorth/pfr) or a folder on this computer.
The card lands in OUT/KEY/ (card.png, card.html, card.json); OUT/compare.html compares it with the reference set.

What happens: Mathlib's declaration table is downloaded on the first run (about 70 MB, cached; if that fails, the
copy included with the tool is used); a git SOURCE is
cloned (history plus current files) into OUT/.clone-KEY and deleted at the end; the project's .lean files are read as
plain text. Nothing from the project is built or executed, and no AI model is involved.
"""
import argparse, signal, urllib.parse, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "proof_anatomy")
REF = os.path.join(HERE, "reference")
sys.path.insert(0, PKG)
import extract as X          # noqa: E402  (comment stripping, for lakefile.lean)
import mathlib_data as D     # noqa: E402
import card as CARD          # noqa: E402


def step(msg):
    print(f"\n== {msg}", flush=True)


MEM_GB = {"extract.py": 4, "card.py": 8, "corpus.py": 4, "git": 4}    # per process (each extraction worker has its own)
CLONE_SECONDS, FILE_GB = 1800, 2


def limits(mem_gb, fsize_gb=None):
    """Hard limits for a child process on Linux: address space (memory) and, for git, the size of any file written.
    A child that needs more fails, and the run stops with a message. Elsewhere the per-file budgets still apply."""
    if not sys.platform.startswith("linux"): return None

    def f():                                        # soft limits, so that card.py can lift them for the browser only
        import resource
        for lim, gb in ((resource.RLIMIT_AS, mem_gb), (resource.RLIMIT_FSIZE, fsize_gb)):
            if gb:
                hard = resource.getrlimit(lim)[1]
                want = gb * 2**30 if hard == resource.RLIM_INFINITY else min(gb * 2**30, hard)
                resource.setrlimit(lim, (want, hard))
    return f


CLONE_MAX_BYTES, CLONE_MAX_FILES = 5 * 2**30, 1_000_000           # on disk, for a clone with its checkout
META_FILES = ("lakefile.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json")   # read at the root only
CHECKOUT_MAX_BYTES = 4 * 2**30
MAX_WRITE_DIRS, BLOCK = 100_000, 4096    # folders the tool may create for one project; allocation unit counted per item


def check_layout(files, used_bytes=0, used_entries=0):
    """Refuse before writing anything if the selected files would need more than MAX_WRITE_DIRS folders, or more than
    CHECKOUT_MAX_BYTES of disk once every file and folder is rounded up to whole blocks, or would take the whole clone
    folder (already holding used_bytes in used_entries files and folders) past CLONE_MAX_BYTES or CLONE_MAX_FILES."""
    dirs = set()
    for path, _, _ in files:
        parts = path.split("/")[:-1]
        for k in range(len(parts), 0, -1):
            d = "/".join(parts[:k])
            if d in dirs: break                       # its parents are already counted
            dirs.add(d)
            if len(dirs) > MAX_WRITE_DIRS:
                refuse(f"the Lean files would need more than {MAX_WRITE_DIRS:,} folders")
    alloc = sum(-(-s // BLOCK) * BLOCK + BLOCK for _, _, s in files) + (len(dirs) + 1) * BLOCK
    if alloc > CHECKOUT_MAX_BYTES or used_bytes + alloc > CLONE_MAX_BYTES:
        refuse(f"the Lean files of this commit would take {alloc / 2**30:.1f} GB on disk, {(used_bytes + alloc) / 2**30:.1f} GB "
               f"with git's own data; the limits are {CHECKOUT_MAX_BYTES // 2**30} and {CLONE_MAX_BYTES // 2**30} GB")
    if used_entries + len(files) + len(dirs) > CLONE_MAX_FILES:
        refuse(f"the clone would hold more than {CLONE_MAX_FILES:,} files and folders")
    return len(dirs), alloc


def select_files(ls_tree):
    """The files the tool needs, from `git ls-tree -r -z -l` (mode type object size<TAB>path, NUL-separated): regular
    files (mode 100644 or 100755) whose path ends in .lean, plus the metadata files at the root. Paths in hidden
    folders are left out (the reader skips them anyway). Returns [(path, object id, size)]; refuses odd paths."""
    out = []
    for rec in ls_tree.split("\0"):
        if "\t" not in rec: continue
        meta, path = rec.split("\t", 1)
        f = meta.split()
        if len(f) != 4 or f[1] != "blob" or f[0] not in ("100644", "100755") or not f[3].isdigit(): continue
        if not (path.endswith(".lean") or path in META_FILES): continue
        parts = path.split("/")
        if any(x in ("", ".", "..") for x in parts) or "\\" in path or len(path) > 4096:
            refuse(f"the repository has a path the tool does not accept: {path[:100]!r}")
        if any(x.startswith(".") for x in parts): continue
        if not re.fullmatch(r"[0-9a-f]{40}", f[2]): continue
        out.append((path, f[2], int(f[3])))
    return out


def write_blobs(gitdir, files, dest, deadline):
    """Write each selected file's stored bytes (no checkout: no attributes, filters or line-ending conversion) under
    dest, a fresh folder. Files are created exclusively and never through links."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **CARD.GIT_OFFLINE_ENV}
    p = subprocess.Popen(["git", *SAFE_GIT, "-c", "protocol.allow=never", "cat-file", "--batch"], cwd=gitdir, env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
                         preexec_fn=limits(MEM_GB["git"], FILE_GB))
    import threading
    def stop():
        try:
            if hasattr(os, "killpg"): os.killpg(p.pid, signal.SIGKILL)
            else: p.kill()
        except (ProcessLookupError, PermissionError):
            pass
    watchdog = threading.Timer(max(1.0, deadline - time.time()), stop); watchdog.start()
    try:
        for path, oid, size in files:
            p.stdin.write((oid + "\n").encode()); p.stdin.flush()
            head = p.stdout.readline().split()
            if len(head) != 3 or head[1] != b"blob" or head[2] != str(size).encode():
                refuse("git did not return the expected file contents (or the time limit was reached)")
            target = os.path.join(dest, *path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as fh:
                left = size
                while left:
                    chunk = p.stdout.read(min(left, 1 << 20))
                    if not chunk: refuse("git stopped while writing the project's files")
                    fh.write(chunk); left -= len(chunk)
            p.stdout.read(1)                                             # the newline after each object
    except OSError as e:
        refuse(f"could not write the project's files ({e.__class__.__name__})")
    finally:
        watchdog.cancel(); stop(); p.wait()


def disk_use(folder, max_bytes, max_files):
    """(files, bytes) under folder, stopping early once either limit is passed."""
    n = size = 0
    end = time.time() + 2.0                          # a scan never holds up the deadline for more than about 2 seconds
    try:
        for _, e, _ in entries(folder):
            n += 1
            try:
                st = e.stat(follow_symlinks=False)
                size += getattr(st, "st_blocks", 0) * 512 or st.st_size     # space allocated on disk
            except OSError: pass
            if n > max_files or size > max_bytes: break
            if time.time() > end: return max_files + 1, size  # too slow to check: treated as over the limit
    except SystemExit:                               # deeper than MAX_DEPTH: count it as over the limit
        return max_files + 1, size
    return n, size


def run_limited(cmd, name, timeout, mem_gb, watch=None, fsize_gb=None, **kw):
    """Run cmd in its own process group under a memory limit (Linux) and a deadline; with watch, also stop it once that
    folder passes CLONE_MAX_BYTES or CLONE_MAX_FILES. Returns (returncode, stdout)."""
    p = subprocess.Popen(cmd, start_new_session=True, preexec_fn=limits(mem_gb, fsize_gb), **kw)
    try:
        return _supervise(p, name, timeout, watch)
    finally:                                         # always: no process of the step may outlive it
        if hasattr(os, "killpg"):
            try: os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
        elif p.poll() is None:
            p.kill()
        p.wait()


def _supervise(p, name, timeout, watch):
    end, why = time.time() + timeout, None
    too_big = f"the project takes more than {CLONE_MAX_BYTES // 2**30} GB or {CLONE_MAX_FILES:,} files on disk"
    over = lambda: watch and any(x > m for x, m in zip(disk_use(watch, CLONE_MAX_BYTES, CLONE_MAX_FILES),
                                                     (CLONE_MAX_FILES, CLONE_MAX_BYTES)))
    while True:
        try:
            out, _ = p.communicate(timeout=min(0.5, max(0.1, end - time.time())))
            if over(): refuse(too_big)                               # also when git finished between two checks
            return p.returncode, out
        except subprocess.TimeoutExpired:
            if time.time() >= end: why = f"{name} did not finish within {timeout // 60} minutes"
            elif over(): why = too_big
            if why:
                if hasattr(os, "killpg"): os.killpg(p.pid, signal.SIGKILL)
                else: p.kill()
                p.wait(); refuse(why)


FSIZE_GB = {"extract.py": 4, "card.py": 1, "corpus.py": 1}         # largest file each step may write


def py(script, *args, timeout=1800):
    rc, _ = run_limited([sys.executable, os.path.join(PKG, script), *args], script, timeout, MEM_GB[script],
                        fsize_gb=FSIZE_GB[script])
    if rc != 0:
        sys.exit(f"stopped: {script} did not finish (see the message above)")


def repo_id(u):
    """A repository link reduced for comparing two links: no scheme, "www.", ".git" or trailing slash; lower case
    (GitHub, GitLab and Codeberg ignore case in owner and repository names)."""
    u = re.sub(r"^[a-z]+://", "", (u or "").strip().lower())
    return re.sub(r"(\.git)?/*$", "", re.sub(r"^www\.", "", u))


def public_url(u):
    """The source URL without user name, password, token, query or fragment, for printing and for the card."""
    if not re.match(r"^(https?|ssh)://", u): return u
    try:
        p = urllib.parse.urlsplit(u)
        host = (p.hostname or "") + (f":{p.port}" if p.port else "")
    except ValueError:
        return re.sub(r"//[^/@]*@", "//", u.split("?")[0].split("#")[0])
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))


OUTPUT_FILES = ("card.json", "card.html", "card_png.html", "card.png", "card.png.tmp.png", "files.jsonl.gz")


def refuse_links(*paths):
    """Outputs are never written through a symbolic link (a planted link could point at any file of the user's)."""
    for p in paths:
        if os.path.islink(p): sys.exit(f"refusing to write through a symbolic link: {p}")


# Strict input policy: anything unusual is refused rather than handled. A refusal names the rule; the README lists them.
PLAIN_PATH = re.compile(r"[\w][\w.+-]*(?:/[\w][\w.+-]*)*")      # relative, no "..", no leading "-" or "."
PLAIN_NAME = re.compile(r"[\w][\w'!?.]*")                        # a Lean name such as Foo.bar_baz'
MAX_LEAN_FILES, MAX_LEAN_BYTES = 500_000, 4 * 2**30
MAX_ENTRIES, SURVEY_SECONDS = 2_000_000, 600      # files and folders listed in the folder that is read
SKIP_DIRS = (".lake", "lake-packages", "build")


def refuse(msg):
    sys.exit(f"refused: {msg}")


def plain_path(s):
    return bool(PLAIN_PATH.fullmatch(s)) and ".." not in s.split("/")


MAX_DEPTH = 64                    # folder levels below the folder that is read (real projects use fewer than 15)


def entries(top, prune=False):
    """Yield (relative path, os.DirEntry, depth) under top, one entry at a time and without recursion (os.walk lists a
    whole folder before yielding and recurses). With prune, hidden folders and SKIP_DIRS are not entered, as in
    extract.py. Folders deeper than MAX_DEPTH are refused."""
    stack = [("", 0)]
    while stack:
        rel, depth = stack.pop()
        try:
            it = os.scandir(os.path.join(top, rel) if rel else top)
        except OSError:
            continue
        with it:
            for e in it:
                r = rel + "/" + e.name if rel else e.name
                yield r, e, depth
                if e.is_dir(follow_symlinks=False) and not (prune and (e.name.startswith(".") or e.name in SKIP_DIRS)):
                    if depth + 1 > MAX_DEPTH: refuse(f"folders nested more than {MAX_DEPTH} levels deep")
                    stack.append((r, depth + 1))


def survey(project, local):
    """Walk the folder the tool reads (`project` is the source folder), pruned exactly as extract.py prunes it. In a
    local folder no symbolic link is allowed there (a clone has none: links are checked out as plain files). Refuses
    more than MAX_LEAN_FILES Lean files or MAX_LEAN_BYTES of Lean code."""
    if local and os.path.islink(project): refuse("the source folder is a symbolic link")
    n = size = count = 0
    cands, end = [], time.time() + SURVEY_SECONDS
    for rel, e, _ in entries(project, prune=True):
        count += 1
        if count > MAX_ENTRIES or time.time() > end:
            refuse(f"the folder has more than {MAX_ENTRIES:,} files and folders, or takes over {SURVEY_SECONDS // 60} minutes to list")
        if local and e.is_symlink(): refuse(f"the folder contains a symbolic link ({rel})")
        if e.name.endswith(".lean") and e.is_file(follow_symlinks=False):
            n += 1; size += e.stat(follow_symlinks=False).st_size
            if len(cands) < 10 and e.name.lower().startswith("main"): cands.append(rel)
            if n > MAX_LEAN_FILES or size > MAX_LEAN_BYTES:
                refuse(f"more than {MAX_LEAN_FILES:,} Lean files or {MAX_LEAN_BYTES // 2**30} GB of Lean code")
    if time.time() > end: refuse(f"the folder takes over {SURVEY_SECONDS // 60} minutes to list")
    return n, cands


ALLOWED_HOSTS = ("github.com", "gitlab.com", "codeberg.org")


def check_source(source):
    """A git source must be a plain https:// link to a public repository on one of ALLOWED_HOSTS (hosts that limit file
    and repository sizes); other schemes (ssh, git@, file://, ext::) and anything with a user name, token, port, '?',
    '#', '%', spaces or control characters are refused."""
    if source.startswith("https://"):
        bad = re.search(r"[\s\x00-\x1f\x7f@?#%\\]", source)
        p = urllib.parse.urlsplit(source)
        try:
            port = p.port
        except ValueError:
            port = -1
        if bad or port is not None or p.netloc not in ALLOWED_HOSTS or not re.fullmatch(r"(/[\w.~+-]+)+/?", p.path):
            refuse(f"the link must be a plain https:// address of a public repository on {', '.join(ALLOWED_HOSTS)} "
                   "(no user name, token, port, '?' or '#'); for other places, download the project and give its folder")
        return True
    if (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source) or re.match(r"^[\w.-]+@[\w.-]+:", source)) and not os.path.isdir(source):
        refuse("only https:// git links or local folders are accepted")
    return False


def default_name(source, is_git):
    """Repository name for the default title and folder, taken from the cleaned URL (never its user, token or query)."""
    if not is_git: return os.path.basename(os.path.abspath(source)) or "project"
    clean = public_url(source)
    last = (urllib.parse.urlsplit(clean).path if re.match(r"^(https?|ssh)://", clean) else clean.split(":")[-1])
    return re.sub(r"\.git$", "", last.rstrip("/").split("/")[-1]) or "project"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "project"


SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "log.showSignature=false",
            "-c", "protocol.file.allow=user", "-c", "core.symlinks=false"]     # the repository's config cannot launch programs


def git(*args, cwd=None, capture=False, offline=False, watch=None):
    """offline: for reading a repository that may be untrusted; no transport may start (see card.GIT_OFFLINE_ENV).
    watch: the clone folder, whose size on disk is limited while git writes to it."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(CARD.GIT_OFFLINE_ENV if offline else {})}
    if offline: args = ("-c", "protocol.allow=never", *args)
    rc, out = run_limited(["git", *SAFE_GIT, *args], "git", CLONE_SECONDS, MEM_GB["git"], watch=watch, fsize_gb=FILE_GB,
                          cwd=cwd, env=env, text=True, errors="replace", stdout=subprocess.PIPE if capture else None)
    if rc: raise subprocess.CalledProcessError(rc, ["git", *args])
    return (out or "").strip() if capture else None


def module_paths(mods):
    """Lean module names or globs -> include entries. `A.B` with a trailing `.+`, `.*` or `.**` (or a `roots` entry)
    covers A/B.lean and A/B/…; an entry marked one: (a Lake `.one` glob) covers A/B.lean only."""
    out = []
    for m in mods:
        one = m.startswith("one:")
        m = re.sub(r"\.(\+|\*|\*\*)$", "", m[4:] if one else m)
        m = m.strip().strip("`").replace("«", "").replace("»", "").rstrip("./")
        p = m.replace(".", "/") + (".lean" if one else "")
        if m and p not in out: out.append(p)
    return out


def toml_uncomment(text):
    """TOML without # comments (outside quoted strings)."""
    out = []
    for line in text.split("\n"):
        q, cut = None, len(line)
        for k, c in enumerate(line):
            if q:
                if c == q: q = None
            elif c in "\"'": q = c
            elif c == "#": cut = k; break
        out.append(line[:cut])
    return "\n".join(out)


QSTR = r"""(?:"([^"]*)"|'([^']*)')"""                            # a TOML string, double or single quoted


def _q(m, g=1):
    return m.group(g) if m.group(g) is not None else m.group(g + 1)


def lean_comments_out(text):
    """lakefile.lean without comments, strings kept (unlike extract.strip_comments, which empties them)."""
    out, i, n = [], 0, len(text)
    while i < n:
        m = re.compile(r'--|/-|"').search(text, i)
        if not m: out.append(text[i:]); break
        out.append(text[i:m.start()])
        if m.group() == '"':
            mm = X.STR.match(text, m.start()); j = mm.end() if mm else n; out.append(text[m.start():j]); i = j
        elif m.group() == "--":
            j = text.find("\n", m.start()); i = n if j < 0 else j
        else:
            i = X._block_end(text, m.start() + 2); out.append(" ")
    return "".join(out)


LAKEFILE_BYTES = 32 * 1024        # real lakefiles are a few kilobytes


def lakefile_text(project, name):
    t = CARD.read_project_file(project, name, limit=LAKEFILE_BYTES + 1) or ""
    if len(t) > LAKEFILE_BYTES: refuse(f"the {name} is larger than {LAKEFILE_BYTES // 1024} KB; pass --src-dir and --include")
    return t


def lake_layout(project):
    """(srcDir, include roots) read from lakefile.toml or lakefile.lean; ("", []) when neither is found."""
    t, l = os.path.join(project, "lakefile.toml"), os.path.join(project, "lakefile.lean")
    src, mods = "", []
    lib_dirs = []
    if os.path.exists(t):
        text = toml_uncomment(lakefile_text(project, "lakefile.toml"))
        head = re.split(r"(?m)^[ \t]*\[", text, maxsplit=1)[0]                 # package keys precede the first table
        m = re.search(rf'(?m)^[ \t]*srcDir[ \t]*=[ \t]*{QSTR}', head)
        if m: src = _q(m)
        for block in re.split(r"(?m)^[ \t]*\[\[lean_lib\]\][ \t]*$", text)[1:]:
            block = re.split(r"(?m)^[ \t]*\[", block, maxsplit=1)[0]
            found = []
            for key in ("roots", "globs"):
                mm = re.search(rf'(?ms)^[ \t]*{key}[ \t]*=[ \t]*\[(.*?)\]', block)
                if mm: found += [_q(x) if key == "roots" or re.search(r"\.[+*]+$", _q(x)) else "one:" + _q(x)
                                 for x in re.finditer(QSTR, mm.group(1))]
            nm = re.search(rf'(?m)^[ \t]*name[ \t]*=[ \t]*{QSTR}', block)
            ld = re.search(rf'(?m)^[ \t]*srcDir[ \t]*=[ \t]*{QSTR}', block)
            lib_dirs.append(_q(ld) if ld else "")
            mods += found or ([_q(nm)] if nm else [])
    elif os.path.exists(l):
        text = lean_comments_out(lakefile_text(project, "lakefile.lean"))
        pk = re.search(r"(?m)^[ \t]*package\b[^\n]*\n?((?:[ \t]+[^\n]*\n?)*)", text)   # the package block only
        m = re.search(r'srcDir[ \t]*:=[ \t]*"([^"]*)"', (pk.group(0) if pk else ""))
        if m: src = m.group(1)
        for mm in re.finditer(r"(?m)^[ \t]*(?:@\[[^\]]*\][ \t]*)?lean_lib[ \t]+(«[^»]+»|[\w.']+)", text):
            end = re.search(r"(?m)^\S", text[mm.end():])                          # the block ends at the next unindented line
            block = text[mm.end(): mm.end() + end.start()] if end else text[mm.end():]
            found = []
            kk = re.search(r"roots[ \t]*:=[ \t]*#\[(.*?)\]", block, re.S)
            if kk: found += re.findall(r"`(«[^»]+»|[\w.']+)", kk.group(1))
            kk = re.search(r"globs[ \t]*:=[ \t]*#\[(.*?)\]", block, re.S)
            if kk:
                for g in re.finditer(r"(\.one|\.submodules|\.andSubmodules)?[ \t]*`(«[^»]+»|[\w.']+)", kk.group(1)):
                    found.append(("one:" if g.group(1) == ".one" else "") + g.group(2))
            ld = re.search(r'srcDir[ \t]*:=[ \t]*"([^"]*)"', block)
            lib_dirs.append(ld.group(1) if ld else "")
            mods += found or [mm.group(1)]
    norm = lambda s: re.sub(r"^(\./)+", "", s.strip()).rstrip("/") if s.strip() not in (".", "./") else ""
    src, lib_dirs = norm(src), [norm(d) for d in lib_dirs]
    bad_dir = next((s for s in [src] + lib_dirs if s and not plain_path(s)), None)
    lib = set(lib_dirs)
    if len(lib) == 1 and next(iter(lib)):                                     # every library sets the same srcDir
        src = "/".join(x for x in (src, next(iter(lib))) if x)
    elif len(lib) > 1:
        print("note: the libraries use different source folders; counting from the package folder", flush=True)
    paths = module_paths(mods)
    bad_lib = next((p for p in paths if not plain_path(p)), None)
    return (None if bad_dir else src), (None if bad_lib else paths)


def main():
    ap = argparse.ArgumentParser(description="Make a Proof Anatomy card for a Lean 4 project.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("source", help="git URL or local folder of the Lean project")
    ap.add_argument("--main", default="", help="file holding the main theorem (relative to the project, e.g. PFR/Main.lean)")
    ap.add_argument("--decl", nargs="*", default=[], help="name(s) of the main theorem(s) in that file; default: the theorems in it, up to 4")
    ap.add_argument("--title", help="plain-language title for the card (default: the repository name)")
    ap.add_argument("--short", help="short name used in comparisons")
    ap.add_argument("--key", help="folder name for the card (default: from the repository name)")
    ap.add_argument("--out", default="cards", help="output folder (default: ./cards)")
    ap.add_argument("--include", nargs="*", help="top-level folders/files to count, relative to the source folder "
                                                  "(default: the lean_lib roots in the lakefile; all .lean files if none)")
    ap.add_argument("--src-dir", help="folder holding the Lean sources, relative to the project (default: the lakefile's srcDir)")
    ap.add_argument("--rev", help="branch, tag or commit to check out (git sources)")
    ap.add_argument("--url", help="link printed on the card (default: the git URL)")
    ap.add_argument("--no-history", action="store_true", help="leave out the commit history")
    ap.add_argument("--no-compare", action="store_true", help="do not compare with the reference set")
    ap.add_argument("--no-png", action="store_true", help="skip card.png (no browser needed)")
    ap.add_argument("--keep-clone", action="store_true", help="keep the cloned repository")
    ap.add_argument("--keep-records", action="store_true", help="keep the per-file records (files.jsonl.gz)")
    ap.add_argument("--procs", type=int, default=max(1, min(5, (os.cpu_count() or 2) // 2)), help="worker processes")
    ap.add_argument("--refresh-data", action="store_true", help="download Mathlib's tables again")
    a = ap.parse_args()
    t0 = time.time()
    if limits(4): limits(4)()                     # this process walks the project's folders: the same memory limit
    is_git = check_source(a.source)
    name = default_name(a.source, is_git)
    if a.main and not (plain_path(a.main.replace("\\", "/")) and a.main.endswith(".lean")):
        refuse(f"--main must be a plain relative path to a .lean file: {a.main!r}")
    for d in a.decl:
        if not PLAIN_NAME.fullmatch(d): refuse(f"--decl must be plain Lean names: {d!r}")
    if a.url is not None and re.search(r"[\x00-\x1f\x7f@?#]", a.url):
        refuse("--url must not contain '@', '?', '#' or control characters")
    for x in (a.include or []) + ([a.src_dir] if a.src_dir else []):
        if not plain_path(x.rstrip("/")): refuse(f"--include and --src-dir take plain relative paths: {x!r}")
    key = a.key or slug(name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key) or ".." in key:
        sys.exit(f"--key must be a simple folder name (letters, digits, . _ -): {key!r}")
    if a.rev and a.rev.startswith("-"):
        sys.exit(f"--rev must be a branch, tag or commit: {a.rev!r}")
    out = os.path.abspath(a.out)
    if not is_git:
        src_abs = os.path.abspath(a.source)
        within = lambda p, b: p == b or p.startswith(b + os.sep)
        if within(out, src_abs) or within(os.path.realpath(out), os.path.realpath(src_abs)):
            refuse("the output folder must not be inside the project; pass --out with a folder elsewhere")
    os.makedirs(out, exist_ok=True)
    card_dir = os.path.join(out, key)
    refuse_links(card_dir)
    os.makedirs(card_dir, exist_ok=True)
    refuse_links(*(os.path.join(card_dir, f) for f in OUTPUT_FILES))

    step("1/4 Mathlib's tables")
    _, meta = D.ensure(refresh=a.refresh_data, retry=True)
    tbl = meta.get("declarations", {})
    print(f"declaration table {'included with the tool, ' if tbl.get('bundled') else ''}downloaded on "
          f"{tbl.get('fetched', '?')} (cache: {D.data_dir()})")

    clone = None
    try:
        if is_git:
            step(f"2/4 cloning {public_url(a.source)}")
            clone = tempfile.mkdtemp(prefix=f".clone-{key}-", dir=out)       # a fresh folder owned by this run
            project, hist = os.path.join(clone, "repo"), os.path.join(clone, "history.git")
            # 1. history and folder lists only, no file contents, no working tree
            git("clone", "-q", "--bare", "--filter=blob:none", *([] if a.rev else ["--single-branch"]), a.source, hist, watch=clone)
            try:
                commit = git("rev-parse", "--verify", "-q", f"{a.rev or 'HEAD'}^{{commit}}", cwd=hist, capture=True, offline=True)
            except subprocess.CalledProcessError:
                refuse(f"no commit named {(a.rev or 'HEAD')!r} in the repository")
            if not re.fullmatch(r"[0-9a-f]{40}", commit): refuse("could not resolve the commit")
            git("update-ref", "--no-deref", "HEAD", commit, cwd=hist, offline=True)     # history up to that commit
            listing = git("ls-tree", "-r", "-z", "--name-only", commit, cwd=hist, capture=True, offline=True)
            paths = [x for x in listing.split("\0") if x]
            if any(x.count("/") > MAX_DEPTH or len(x) > 4096 for x in paths):
                refuse(f"the repository has paths nested more than {MAX_DEPTH} levels deep or longer than 4,096 characters")
            n_lp = sum(x.endswith(".lean") for x in paths)
            if len(paths) > CLONE_MAX_FILES or n_lp > MAX_LEAN_FILES:
                refuse(f"the repository has {len(paths):,} files ({n_lp:,} Lean files); the limits are "
                       f"{CLONE_MAX_FILES:,} and {MAX_LEAN_FILES:,}")
            # 2. that one commit's contents, kept packed inside git (nothing written out yet)
            content = os.path.join(clone, "content.git")
            git("init", "-q", "--bare", content)
            git("fetch", "-q", "--depth", "1", "--no-tags", a.source, commit, cwd=content, watch=clone)
            # 3. the tool picks the files, sums their exact sizes, and writes only those bytes (no git checkout)
            files = select_files(git("ls-tree", "-r", "-z", "-l", commit, cwd=content, capture=True, offline=True))
            used_n, used_b = disk_use(clone, CLONE_MAX_BYTES, CLONE_MAX_FILES)   # what git has stored so far
            check_layout(files, used_b, used_n)       # file bytes, folders, block rounding and git's data, before writing
            os.makedirs(project)
            write_blobs(content, files, project, time.time() + CLONE_SECONDS)
        else:
            step(f"2/4 using the folder {a.source}")
            project = os.path.abspath(a.source)
            if not os.path.isdir(project): sys.exit(f"not a folder: {a.source}")
            print("git is not run on local folders, so the card has no commit history (it does for https:// links)")
        if not is_git: commit = hist = None      # git is never run on a local folder
        try:
            src_dir, libs = X.limited(lake_layout, project)   # None: the lakefile's value is not plain (refused if used)
        except X.TooComplex:
            refuse("the lakefile takes too long to read")
        if a.src_dir is not None: src_dir = a.src_dir.strip("/")         # checked to be plain above
        if src_dir is None: refuse("the lakefile's source folder is not a plain folder name; pass --src-dir")
        if libs is None and a.include is None: refuse("the lakefile names libraries that are not plain names; pass --include")
        root = os.path.join(project, src_dir) if src_dir else project
        inside = lambda p: os.path.realpath(p) == os.path.realpath(project) or \
            os.path.realpath(p).startswith(os.path.realpath(project) + os.sep)
        if not inside(root):                       # a lakefile (written by the project's authors) must not point elsewhere
            sys.exit(f"the source folder {src_dir!r} is outside the project; pass --src-dir with a folder inside it")
        n_lean, main_cands = survey(root, local=not is_git)   # exactly the folder that is read, pruned like extract.py
        deadline = 900 + n_lean // 20                # seconds per step: ~2.6 hours for 175,000 files (which took 4 minutes)
        include = a.include if a.include is not None else libs
        print(f"source folder: {src_dir or '(project root)'}; counting: {', '.join(include) if include else 'all .lean files'}"
              + ("" if a.include is not None else " (from the lakefile)" if libs else ""))
        main_file = a.main.replace("\\", "/")
        if main_file and not inside(os.path.join(root, main_file)):
            sys.exit(f"--main must be a file inside the project: {a.main!r}")
        if main_file and src_dir and main_file.startswith(src_dir + "/"): main_file = main_file[len(src_dir) + 1:]
        if main_file and not os.path.isfile(os.path.join(root, main_file)):
            sys.exit(f"main file not found: {main_file} (under {src_dir or 'the project root'}). Files named Main*: {main_cands}")
        if not main_file:
            print("no --main given: the card will not describe the main theorem's statement")

        step(f"3/4 reading the .lean files ({a.procs} processes)")
        # every value goes in --opt=value form, so a value written by the project's authors (a lakefile entry such as
        # "--root") or chosen from its README can never be read as an option
        py("extract.py", f"--root={root}", f"--out={card_dir}", f"--procs={a.procs}", *(f"--include={x}" for x in include),
           timeout=deadline)
        url = public_url(a.url) if a.url is not None else (re.sub(r"^https?://|\.git$", "", public_url(a.source)) if is_git else "")
        cmd = [f"--data={card_dir}", f"--root={root}", f"--title={a.title or name}", f"--url={url}", f"--project-dir={project}"]
        if a.short: cmd += [f"--short={a.short}"]
        if main_file: cmd += [f"--main={main_file}"]
        cmd += [f"--decl={d}" for d in a.decl]
        if commit: cmd += [f"--commit={commit}"]
        if is_git and not a.no_history: cmd += [f"--git={hist}"]
        if a.no_png or not a.no_compare: cmd += ["--no-png"]          # the comparison step renders the PNG
        py("card.py", *cmd, timeout=deadline)
    finally:
        if clone and not a.keep_clone: shutil.rmtree(clone, ignore_errors=True)        # only the folder this run created
        rec = os.path.join(card_dir, "files.jsonl.gz")
        if os.path.exists(rec) and not a.keep_records: os.remove(rec)

    if not a.no_compare:
        step("4/4 comparing with the reference set")
        mine = {repo_id(x) for x in (url, public_url(a.source) if is_git else "") if repo_id(x)}
        counted = {x.strip("/") for x in include} | {f"{src_dir}/{x}".strip("/") for x in include}

        def same_project(p):
            """The same repository; for a reference project that is single files in a larger repository, also the same
            files (another problem from the same collection is a different project)."""
            files = [x for x in p.get("include") or [] if x.endswith(".lean")]
            return repo_id(p.get("url")) in mine and (not files or any(f.strip("/") in counted for f in files))
        refs, same = [], []
        for p in json.load(open(os.path.join(REF, "projects.json"), encoding="utf-8")):
            if p["key"] == key: continue                               # this run's card replaces the reference copy
            (same if same_project(p) else refs).append(p["key"])
        if same: print(f"left out of the comparison: the reference card for the same project ({', '.join(same)})", flush=True)
        refuse_links(*(os.path.join(out, f) for f in ("corpus.html", "compare.html", "corpus.json")))
        for k in refs:
            refuse_links(os.path.join(out, k))
            os.makedirs(os.path.join(out, k), exist_ok=True)
            refuse_links(*(os.path.join(out, k, f) for f in OUTPUT_FILES))
            shutil.copy(os.path.join(REF, "cards", k, "card.json"), os.path.join(out, k, "card.json"))
        py("corpus.py", out, key, *refs, *(["--png"] if a.no_png else ["--png", key]))
        if os.path.exists(os.path.join(out, "corpus.html")):
            os.replace(os.path.join(out, "corpus.html"), os.path.join(out, "compare.html"))
    else:
        step("4/4 comparison skipped")

    K = json.load(open(os.path.join(card_dir, "card.json"), encoding="utf-8"))
    n = K["numbers"]
    print(f"\nDone in {time.time() - t0:.0f}s.\n  {n['files']:,} Lean files, {n['code_lines']:,} lines of code, "
          f"{n['theorems']:,} theorems and lemmas, {n['definitions']:,} definitions")
    for s in K["statement"]["sentences"][:1]: print("  " + s)
    if K.get("resemblance"):
        c = K["resemblance"]["closest"][0]; print(f"  closest in its mix of Mathlib areas: {c['short'] or c['title']} ({c['similarity']:.2f})")
    print(f"  qualified names traced: {K['coverage']['resolved_to_mathlib_pct']}%")
    for f in ("card.png", "card.html", "card.json"):
        p = os.path.join(card_dir, f)
        if os.path.exists(p) and os.path.getmtime(p) >= t0 - 1: print("  " + p)     # written by this run
    if os.path.exists(os.path.join(out, "compare.html")) and not a.no_compare: print("  " + os.path.join(out, "compare.html"))


if __name__ == "__main__":
    main()
