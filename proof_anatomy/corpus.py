#!/usr/bin/env python3
"""Proof Anatomy, pass 3: compare cards. Resemblance = cosine similarity of the projects' mixes of Mathlib areas
(second-level folders, e.g. NumberTheory/LSeries; shares of code lines). Families = average-linkage hierarchical
clustering on 1 - similarity. Writes each card's "resemblance" field, re-renders the cards, and writes a comparison page.

Usage: python3 corpus.py OUT_DIR KEY [KEY ...] [--png KEY ...]    (cards in OUT_DIR/KEY/card.json; card.png is
       re-rendered only for the keys after --png, or for all keys when --png is not given)
"""
import html, json, math, os, sys, urllib.parse
import numpy as np
from scipy.cluster.hierarchy import linkage, dendrogram
from scipy.spatial.distance import squareform

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import card as CARD

METHOD = ("Similarity of two projects' mixes of Mathlib areas: the share of code lines attributed to each of Mathlib's "
          "second-level folders (such as NumberTheory/LSeries), compared by cosine similarity; 1 means the same mix, 0 nothing in common. "
          "It compares the foundations the code is built on, not the problems the proofs solve.")


def main():
    args = sys.argv[1:]
    png = None
    if "--png" in args:
        i = args.index("--png"); png = set(args[i + 1:]); args = args[:i]
    out, keys = args[0], args[1:]
    K = {k: json.load(open(os.path.join(out, k, "card.json"), encoding="utf-8")) for k in keys}
    empty = [k for k in keys if sum(v for a, v in K[k].get("areas_L2", {}).items() if a != CARD.OUTSIDE) <= 0]
    for k in empty:                                   # no Mathlib areas (only Lean core and other libraries): nothing to compare
        K[k]["resemblance"] = None
        with open(os.path.join(out, k, "card.json"), "w", encoding="utf-8") as f:
            json.dump(K[k], f, indent=1, ensure_ascii=False)
        CARD.render(os.path.join(out, k), png=png is None or k in png)
        print(f"{k}: no Mathlib areas, left out of the comparison", flush=True)
    keys = [k for k in keys if k not in empty]
    if len(keys) < 2:
        print("fewer than two cards with Mathlib areas: no comparison", flush=True)
        for k in keys:
            K[k]["resemblance"] = None
            with open(os.path.join(out, k, "card.json"), "w", encoding="utf-8") as f:
                json.dump(K[k], f, indent=1, ensure_ascii=False)
            CARD.render(os.path.join(out, k), png=png is None or k in png)
        return
    A = sorted({a for k in keys for a in K[k]["areas_L2"]})
    V = np.array([[K[k]["areas_L2"].get(a, 0.0) for a in A] for k in keys], float)
    V /= V.sum(axis=1, keepdims=True)
    U = V / np.linalg.norm(V, axis=1, keepdims=True)
    S = np.clip(U @ U.T, 0, 1)
    D = squareform(1 - S, checks=False)
    Z = linkage(D, method="average")
    dn = dendrogram(Z, no_plot=True, labels=keys)
    for i, k in enumerate(keys):
        others = sorted(((S[i, j], keys[j]) for j in range(len(keys)) if j != i), reverse=True)
        K[k]["resemblance"] = {"compared": len(keys) - 1, "method": METHOD,
                               "closest": [{"key": kk, "title": K[kk]["title"], "short": K[kk].get("short"), "similarity": round(float(s), 3)} for s, kk in others]}
        with open(os.path.join(out, k, "card.json"), "w", encoding="utf-8") as f:
            json.dump(K[k], f, indent=1, ensure_ascii=False)
        CARD.render(os.path.join(out, k), png=png is None or k in png)
    order = dn["ivl"]
    with open(os.path.join(out, "corpus.json"), "w", encoding="utf-8") as f:
        json.dump({"keys": keys, "similarity": S.round(3).tolist(), "order": order, "linkage": Z.round(4).tolist(), "method": METHOD}, f, indent=1)
    page(out, keys, K, S, Z, order)


def page(out, keys, K, S, Z, order):
    esc = lambda s: html.escape(str(s))
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys); cell = 72; left = 210; top = 120
    nm = lambda k: K[k].get("short") or short(K[k]["title"])
    hm = [f'<svg viewBox="0 0 {left + n * cell + 140} {top + n * cell + 10}" width="100%" role="img" aria-label="Similarity matrix">']
    for c, b in enumerate(order):
        hm.append(f'<text transform="translate({left + c * cell + cell / 2 - 4},{top - 10}) rotate(-35)" text-anchor="start">{esc(nm(b))}</text>')
    for r, a in enumerate(order):
        hm.append(f'<text x="{left - 10}" y="{top + r * cell + cell / 2 + 4}" text-anchor="end">{esc(nm(a))}</text>')
        for c, b in enumerate(order):
            s = S[idx[a], idx[b]]; op = 0.06 + 0.94 * s ** 3; ink = "#fff" if op > 0.72 else "var(--ink)"
            hm.append(f'<rect x="{left + c * cell}" y="{top + r * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="var(--bar)" fill-opacity="{op:.3f}" data-tip="{esc(nm(a))} vs {esc(nm(b))}: {s:.2f}"/>')
            hm.append(f'<text x="{left + c * cell + cell / 2 - 1}" y="{top + r * cell + cell / 2 + 4}" text-anchor="middle" style="fill:{ink}">{s:.2f}</text>')
    hm.append("</svg>")
    # dendrogram as text groups: cut at the largest gap in merge heights
    hts = sorted(Z[:, 2]); gaps = [(hts[i + 1] - hts[i], i) for i in range(len(hts) - 1)]
    if len(keys) >= 3 and gaps and max(gaps)[0] > 0:  # families need at least two merges with a gap between them
        cut = (hts[max(gaps)[1]] + hts[max(gaps)[1] + 1]) / 2
        from scipy.cluster.hierarchy import fcluster
        lab = fcluster(Z, t=cut, criterion="distance")
        fams = {}
        for k, l in zip(keys, lab): fams.setdefault(l, []).append(k)
        fam_html = "<ul>" + "".join(f'<li>{", ".join(esc(K[k].get("short") or short(K[k]["title"])) for k in sorted(v, key=order.index))}</li>' for v in sorted(fams.values(), key=lambda v: order.index(v[0]))) + "</ul>"
    else:
        fam_html = '<p class="note">Families need at least three projects with distinct merge distances.</p>'
    href = lambda k: esc("./" + urllib.parse.quote(k) + "/card.html")
    rows = "".join(f'<tr><td><a href="{href(k)}">{esc(K[k]["title"])}</a></td><td class="n">{K[k]["numbers"]["files"]:,}</td><td class="n">{K[k]["numbers"]["code_lines"]:,}</td>'
                   f'<td class="n">{K[k]["numbers"]["theorems"]:,}</td><td class="n">{K[k]["borrowed"]["own_pct"]:.0f}%</td>'
                   f'<td>{esc(K[K[k]["resemblance"]["closest"][0]["key"]].get("short") or short(K[K[k]["resemblance"]["closest"][0]["key"]]["title"]))} ({K[k]["resemblance"]["closest"][0]["similarity"]:.2f})</td></tr>' for k in order)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lean formalizations compared</title><style>{CARD.CSS}</style></head><body><main>'
           f'<p class="kicker">Proof Anatomy · counted from the source text, no build, no AI judgment</p><h1>{len(keys)} Lean formalizations compared</h1>'
           f'<p class="sub">{esc(METHOD)}</p>'
           f'<table><thead><tr><th>Project</th><th class="n">Files</th><th class="n">Lines</th><th class="n">Theorems</th><th class="n">Own names</th><th>Closest</th></tr></thead><tbody>{rows}</tbody></table>'
           f'<h2>Similarity of their Mathlib mixes</h2><p class="sub">Ordered so that similar projects sit next to each other (average-linkage clustering).</p>{"".join(hm)}'
           f'<h2>Families</h2><p class="sub">Groups from the same clustering, cut at the largest jump between merge distances.</p>{fam_html}'
           f'</main><div id="tip"></div>{CARD.TIPJS}</body></html>')
    with open(os.path.join(out, "corpus.html"), "w", encoding="utf-8") as f: f.write(doc)


def short(t):
    return t.split(" (")[0]


if __name__ == "__main__":
    main()
