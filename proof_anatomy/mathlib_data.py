"""Mathlib reference data, downloaded once and cached.

- Mathlib's published declaration table (the data behind the Mathlib documentation website): every declaration's
  name, kind and module. About 70 MB; it is turned into a compact index and then deleted.
- Mathlib's lists of famous theorems: docs/1000.yaml (the 1000+ theorems project) and docs/100.yaml (Freek Wiedijk's
  100 theorems), which name the Mathlib declaration that proves each theorem.

Cache folder: $PROOF_ANATOMY_DATA, or ~/.cache/proof-anatomy.

If a download fails (no network, or a sandbox that allows only some sites), the copy included with the tool in
proof_anatomy/data/ is used instead and marked as such; anatomy.py tries the download again on its next run.
"""
import datetime, gzip, json, os, shutil, urllib.request

URLS = {
    "declarations": "https://leanprover-community.github.io/mathlib4_docs/declarations/declaration-data.bmp",
    "1000.yaml": "https://raw.githubusercontent.com/leanprover-community/mathlib4/master/docs/1000.yaml",
    "100.yaml": "https://raw.githubusercontent.com/leanprover-community/mathlib4/master/docs/100.yaml",
}
EXCLUDED_ROOTS = {"Archive", "Counterexamples", "Lake"}   # in the docs table, but not brought in by `import Mathlib`
INDEX = "index.json.gz"          # plain data (an earlier version used a pickle, which can run code when loaded)
FILES = {"declarations": INDEX, "1000.yaml": "1000.yaml", "100.yaml": "100.yaml"}    # item -> file in the cache
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")           # the copy included with the tool
MIN_DECLARATIONS = 100_000       # Mathlib has over 400,000; fewer means the download was not the real table


def data_dir():
    d = os.environ.get("PROOF_ANATOMY_DATA") or os.path.join(os.path.expanduser("~"), ".cache", "proof-anatomy")
    os.makedirs(d, exist_ok=True)
    return d


def _part(dest):
    return f"{dest}.{os.getpid()}.part"                            # one per process: concurrent runs never share it


def _download(url, dest):
    tmp = _part(dest)
    req = urllib.request.Request(url, headers={"User-Agent": "proof-anatomy"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp): os.remove(tmp)


def _build_index(json_path, idx_path):
    d = json.load(open(json_path, encoding="utf-8"))["declarations"]
    mods, names, cname, cmod, ckind = {}, {}, [], [], []
    for n, v in d.items():
        m = v["docLink"].split("#")[0][2:-5]                 # "./Mathlib/Data/Nat/Basic.html#x" -> Mathlib/Data/Nat/Basic
        if m.split("/")[0] in EXCLUDED_ROOTS: continue
        names[n] = len(cname); cname.append(n); cmod.append(mods.setdefault(m, len(mods))); ckind.append(v.get("kind"))
    if len(cname) < MIN_DECLARATIONS: raise ValueError(f"only {len(cname)} declarations")
    tmp = _part(idx_path)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"mods": list(mods), "cname": cname, "cmod": cmod, "ckind": ckind}, f, separators=(",", ":"))
        os.replace(tmp, idx_path)
    finally:
        if os.path.exists(tmp): os.remove(tmp)


def _fetch(d, item):
    """Download one item into the cache folder d, checked before it replaces anything."""
    if item == "declarations":
        jp = _part(os.path.join(d, "declaration-data.json"))
        try:
            _download(URLS[item], jp)
            _build_index(jp, os.path.join(d, INDEX))
        finally:
            if os.path.exists(jp): os.remove(jp)
    else:
        import yaml
        dest = os.path.join(d, item); new = _part(dest + ".new")
        try:
            _download(URLS[item], new)
            if not isinstance(yaml.safe_load(open(new, encoding="utf-8")), dict): raise ValueError("not a theorem list")
            os.replace(new, dest)
        finally:
            if os.path.exists(new): os.remove(new)


def _use_bundled(d, item):
    """Copy the included copy of one item into the cache folder d; returns its record (source and date)."""
    src, dest = os.path.join(BUNDLED, FILES[item]), os.path.join(d, FILES[item])
    rec = _read_meta(os.path.join(BUNDLED, "snapshot.json")).get(item)
    if not os.path.isfile(src) or not isinstance(rec, dict):
        raise SystemExit(f"could not download {URLS[item]} and the copy included with the tool is missing; "
                         "check the network connection")
    tmp = _part(dest)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    return {"url": str(rec.get("url", "")), "fetched": str(rec.get("fetched", "")), "bundled": True}


def _read_meta(path):
    """The download records, {item: {url, fetched[, bundled]}}; a damaged file or record is ignored (the data files
    themselves are kept: whether an item is cached depends only on its file)."""
    try:
        m = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in m.items() if isinstance(v, dict)} if isinstance(m, dict) else {}


def _needed(d, meta, refresh, retry):
    """Items to download: all with refresh; missing files; with retry, also those served by the included copy."""
    return [i for i in FILES if refresh or not os.path.exists(os.path.join(d, FILES[i]))
            or (retry and meta.get(i, {}).get("bundled"))]


class _Lock:
    """One cache update at a time (other runs wait), so a run never replaces what another has just installed.
    Waiting is only for another run holding the lock; any other locking error stops with a message."""
    WAIT_SECONDS = 1800

    def __init__(self, d):
        self.fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            if os.name == "nt":
                import errno, msvcrt, time
                busy = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
                end = time.monotonic() + self.WAIT_SECONDS
                while True:
                    try:
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1); break
                    except OSError as e:
                        if e.errno not in busy or time.monotonic() > end: raise
                        time.sleep(0.5)                                # another run holds it
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX)                    # waits only while another run holds it
        except OSError as e:
            os.close(self.fd)
            raise SystemExit(f"could not lock the Mathlib data cache in {d} ({e.__class__.__name__}: {e.strerror}); "
                             "set PROOF_ANATOMY_DATA to a folder on a local disk")
        except BaseException:
            os.close(self.fd); raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            try:
                import msvcrt
                os.lseek(self.fd, 0, 0); msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(self.fd)                                              # releases the lock


def ensure(refresh=False, quiet=False, retry=False):
    """Download what is missing (everything, with refresh; with retry, also what the included copy stands in for).
    A failed download keeps what is cached, or else uses the copy included with the tool.
    Returns (folder, {item: {url, fetched[, bundled]}}).
    Nothing is written when everything is already cached, so concurrent readers never see a half-written file."""
    d = data_dir()
    meta_path = os.path.join(d, "fetched.json")
    meta = _read_meta(meta_path)
    if not _needed(d, meta, refresh, retry): return d, meta        # the usual case: no lock, no writes
    with _Lock(d):
        meta = _read_meta(meta_path)                               # again: another run may have just finished
        before = json.dumps(meta, sort_keys=True)
        today = datetime.date.today().isoformat()
        old = os.path.join(d, "index.pkl")
        if os.path.exists(old): os.remove(old)                     # never loaded; replaced by index.json.gz
        for item in _needed(d, meta, refresh, retry):
            have = os.path.exists(os.path.join(d, FILES[item]))
            if item == "declarations" and not quiet:
                print("downloading Mathlib's declaration table (about 70 MB) ...", flush=True)
            try:
                _fetch(d, item)
                meta[item] = {"url": URLS[item], "fetched": today}
            except Exception as e:                                 # no network, a blocked site, or not the real file
                if not have: meta[item] = _use_bundled(d, item)
                if not quiet:
                    rec = meta.get(item, {})
                    date = rec.get("fetched", "?")
                    which = f"the copy included with the tool (downloaded on {date})" if rec.get("bundled") \
                        else f"the cached copy (downloaded on {date})"
                    print(f"could not download {URLS[item]} ({e.__class__.__name__}); using {which}", flush=True)
        if json.dumps(meta, sort_keys=True) != before:
            tmp = _part(meta_path)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=1)
                os.replace(tmp, meta_path)                         # atomic
            finally:
                if os.path.exists(tmp): os.remove(tmp)
    return d, meta


def load_index():
    d, _ = ensure(quiet=True)
    with gzip.open(os.path.join(d, INDEX), "rt", encoding="utf-8") as f:
        T = json.load(f)
    ok = isinstance(T, dict) and all(isinstance(T.get(k), list) for k in ("mods", "cname", "cmod", "ckind")) \
        and len(T["cname"]) == len(T["cmod"]) == len(T["ckind"])
    if not ok: raise SystemExit(f"the cached Mathlib index in {d} is damaged; run again with --refresh-data")
    T["names"] = {n: i for i, n in enumerate(T["cname"])}
    return T


def path(fn):
    return os.path.join(data_dir(), fn)


def fetched():
    return _read_meta(os.path.join(data_dir(), "fetched.json"))
