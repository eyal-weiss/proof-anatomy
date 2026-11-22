"""Mathlib reference data, downloaded once and cached.

- Mathlib's published declaration table (the data behind the Mathlib documentation website): every declaration's
  name, kind and module. About 70 MB; it is turned into a compact index and then deleted.
- Mathlib's lists of famous theorems: docs/1000.yaml (the 1000+ theorems project) and docs/100.yaml (Freek Wiedijk's
  100 theorems), which name the Mathlib declaration that proves each theorem.

Cache folder: $PROOF_ANATOMY_DATA, or ~/.cache/proof-anatomy.
"""
import datetime, gzip, json, os, shutil, urllib.request

URLS = {
    "declarations": "https://leanprover-community.github.io/mathlib4_docs/declarations/declaration-data.bmp",
    "1000.yaml": "https://raw.githubusercontent.com/leanprover-community/mathlib4/master/docs/1000.yaml",
    "100.yaml": "https://raw.githubusercontent.com/leanprover-community/mathlib4/master/docs/100.yaml",
}
EXCLUDED_ROOTS = {"Archive", "Counterexamples", "Lake"}   # in the docs table, but not brought in by `import Mathlib`
INDEX = "index.json.gz"          # plain data (an earlier version used a pickle, which can run code when loaded)


def data_dir():
    d = os.environ.get("PROOF_ANATOMY_DATA") or os.path.join(os.path.expanduser("~"), ".cache", "proof-anatomy")
    os.makedirs(d, exist_ok=True)
    return d


def _download(url, dest):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "proof-anatomy"})
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)


def _build_index(json_path, idx_path):
    d = json.load(open(json_path, encoding="utf-8"))["declarations"]
    mods, names, cname, cmod, ckind = {}, {}, [], [], []
    for n, v in d.items():
        m = v["docLink"].split("#")[0][2:-5]                 # "./Mathlib/Data/Nat/Basic.html#x" -> Mathlib/Data/Nat/Basic
        if m.split("/")[0] in EXCLUDED_ROOTS: continue
        names[n] = len(cname); cname.append(n); cmod.append(mods.setdefault(m, len(mods))); ckind.append(v.get("kind"))
    with gzip.open(idx_path + ".part", "wt", encoding="utf-8") as f:
        json.dump({"mods": list(mods), "cname": cname, "cmod": cmod, "ckind": ckind}, f, separators=(",", ":"))
    os.replace(idx_path + ".part", idx_path)


def _read_meta(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure(refresh=False, quiet=False):
    """Download what is missing (or everything, with refresh). Returns (folder, {item: {url, fetched}}).
    Nothing is written when everything is already cached, so concurrent readers never see a half-written file."""
    d = data_dir()
    meta_path = os.path.join(d, "fetched.json")
    meta = _read_meta(meta_path)
    before = json.dumps(meta, sort_keys=True)
    today = datetime.date.today().isoformat()
    old = os.path.join(d, "index.pkl")
    if os.path.exists(old): os.remove(old)                         # never loaded; replaced by index.json.gz
    if refresh or not os.path.exists(os.path.join(d, INDEX)):
        if not quiet: print("downloading Mathlib's declaration table (about 70 MB, first run only) ...", flush=True)
        jp = os.path.join(d, "declaration-data.json")
        _download(URLS["declarations"], jp)
        _build_index(jp, os.path.join(d, INDEX))
        os.remove(jp)
        meta["declarations"] = {"url": URLS["declarations"], "fetched": today}
    for fn in ("1000.yaml", "100.yaml"):
        if refresh or not os.path.exists(os.path.join(d, fn)):
            _download(URLS[fn], os.path.join(d, fn))
            meta[fn] = {"url": URLS[fn], "fetched": today}
    if json.dumps(meta, sort_keys=True) != before:
        with open(meta_path + ".part", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        os.replace(meta_path + ".part", meta_path)                 # atomic
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
