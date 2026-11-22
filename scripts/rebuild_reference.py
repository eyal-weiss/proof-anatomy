#!/usr/bin/env python3
"""Rebuild every reference card from source, the comparison, and the website in docs/.

  python3 scripts/rebuild_reference.py [--local rh=PATH ...] [--only KEY ...] [--procs 5]

Projects with a git source are cloned and deleted again by anatomy.py. Projects distributed as archives (the
Riemann Hypothesis proof) need --local KEY=PATH pointing at the extracted project folder (the one with the lakefile);
without it, their existing card is kept. Writes reference/cards/KEY/card.json, docs/index.html, docs/KEY/card.html,
docs/KEY/card.png, and prints the table for README.md.
"""
import argparse, json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(HERE, "reference")
DOCS = os.path.join(HERE, "docs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", nargs="*", default=[], help="KEY=PATH for projects without a git source")
    ap.add_argument("--only", nargs="*", help="rebuild only these keys (the comparison still uses all)")
    ap.add_argument("--procs", type=int, default=5)
    ap.add_argument("--build", default=os.path.join(HERE, "build", "reference"))
    a = ap.parse_args()
    local = dict(x.split("=", 1) for x in a.local)
    P = json.load(open(os.path.join(REF, "projects.json"), encoding="utf-8"))
    os.makedirs(a.build, exist_ok=True)
    times_path = os.path.join(REF, "build_times.json")
    times = json.load(open(times_path, encoding="utf-8")) if os.path.exists(times_path) else {}
    for p in P:
        k = p["key"]
        if a.only and k not in a.only: continue
        src = p["source"] if p["source"] != "archive" else local.get(k)
        if not src:
            print(f"{k}: no source given (--local {k}=PATH); keeping the existing card"); continue
        cmd = [sys.executable, os.path.join(HERE, "anatomy.py"), src, "--key", k, "--out", a.build, "--title", p["title"],
               "--short", p["short"], "--url", p["url"], "--procs", str(a.procs), "--no-compare", "--no-png"]
        if p.get("main"): cmd += ["--main", p["main"]]
        if p.get("decl"): cmd += ["--decl", *p["decl"]]
        if p.get("include") is not None: cmd += ["--include", *p["include"]]     # [] = every .lean file
        if p.get("no_history"): cmd += ["--no-history"]
        t0 = time.time()
        subprocess.run(cmd, check=True)
        times[k] = round(time.time() - t0)
        os.makedirs(os.path.join(REF, "cards", k), exist_ok=True)
        shutil.copy(os.path.join(a.build, k, "card.json"), os.path.join(REF, "cards", k, "card.json"))
    with open(times_path, "w", encoding="utf-8") as f: json.dump(times, f, indent=1)

    # comparison over the whole set, rendered into docs/ (the GitHub Pages site)
    keys = [p["key"] for p in P]
    for k in keys:
        os.makedirs(os.path.join(DOCS, k), exist_ok=True)
        shutil.copy(os.path.join(REF, "cards", k, "card.json"), os.path.join(DOCS, k, "card.json"))
    subprocess.run([sys.executable, os.path.join(HERE, "proof_anatomy", "corpus.py"), DOCS, *keys], check=True)
    os.replace(os.path.join(DOCS, "corpus.html"), os.path.join(DOCS, "index.html"))
    for k in keys:                                    # the reference cards keep their resemblance field
        shutil.copy(os.path.join(DOCS, k, "card.json"), os.path.join(REF, "cards", k, "card.json"))
        tmp = os.path.join(DOCS, k, "card_png.html")
        if os.path.exists(tmp): os.remove(tmp)
    open(os.path.join(DOCS, ".nojekyll"), "w").close()

    print("\n| Project | Files | Lines | Theorems | Own names | Qualified names traced | Time |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for k in keys:
        K = json.load(open(os.path.join(REF, "cards", k, "card.json"), encoding="utf-8")); n = K["numbers"]
        t = times.get(k); ts = "–" if t is None else (f"{t} s" if t < 90 else f"{t / 60:.1f} min")
        print(f"| [{K['short']}](https://eyal-weiss.github.io/proof-anatomy/{k}/card.html) | {n['files']:,} | {n['code_lines']:,} | {n['theorems']:,} | {K['borrowed']['own_pct']:.0f}% | "
              f"{K['coverage']['resolved_to_mathlib_pct']}% | {ts} |")


if __name__ == "__main__":
    main()
