#!/usr/bin/env python3
"""Tests for anatomy.py's lakefile reading and argument checks. Run: python3 tests/test_wrapper.py"""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.argv = ["anatomy.py"]
import anatomy as A

FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: FAILS.append(name)


check("repository links compare equal regardless of scheme, case, .git and trailing slash",
      A.repo_id("https://GitHub.com/teorth/PFR.git/") == A.repo_id("github.com/teorth/pfr") == "github.com/teorth/pfr"
      and A.repo_id("https://github.com/teorth/pfr2") != A.repo_id("github.com/teorth/pfr") and A.repo_id("") == "")


def layout(fn, text):
    d = tempfile.mkdtemp()
    open(os.path.join(d, fn), "w", encoding="utf-8").write(text)
    return A.lake_layout(d)


r = layout("lakefile.lean", 'import Lake\nopen Lake DSL\npackage Demo where\n  srcDir := "src"\nlean_lib Demo\n')
check("lakefile.lean srcDir", r == ("src", ["Demo"]), r)
r = layout("lakefile.lean", 'import Lake\nopen Lake DSL\npackage Demo\n-- lean_lib Fake\n@[default_target]\nlean_lib Main where\n  globs := #[.submodules `Foo, `Bar.*]\n')
check("lakefile.lean globs and comments", r == ("", ["Foo", "Bar"]), r)
r = layout("lakefile.toml", 'name = "demo"\nsrcDir = \'src\'\n\n[[lean_lib]] # main library\nname = \'Demo\'\n')
check("lakefile.toml comment after the table and single quotes", r == ("src", ["Demo"]), r)
r = layout("lakefile.toml", 'name = "tree"\nsrcDir = "src"\n[[lean_lib]]\nname = "G"\nglobs = ["G.A.+", "G.B.+"]\n')
check("lakefile.toml globs", r == ("src", ["G/A", "G/B"]), r)
check("no lakefile", A.lake_layout(tempfile.mkdtemp()) == ("", []))
r = layout("lakefile.toml", 'name = "demo"\n[[lean_lib]]\nname = "Demo"\nsrcDir = "src"\n')
check("lakefile.toml library srcDir", r == ("src", ["Demo"]), r)
r = layout("lakefile.toml", 'name = "demo"\n[[lean_lib]]\nname = "Demo"\nroots = [\n  # "Tests",\n  "Demo"\n]\n')
check("lakefile.toml commented-out array entry", r == ("", ["Demo"]), r)
r = layout("lakefile.lean", 'import Lake\nopen Lake DSL\npackage Demo\nlean_lib Demo where\n  globs := #[.one `Foo]\n')
check("lakefile.lean .one glob selects one file", r == ("", ["Foo.lean"]), r)
r = layout("lakefile.lean", 'import Lake\nopen Lake DSL\npackage Demo\nlean_lib Demo where\n  srcDir := "lib"\n')
check("lakefile.lean library srcDir", r == ("lib", ["Demo"]), r)

out = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), tempfile.mkdtemp(), "--key", "../../../victim",
                      "--out", tempfile.mkdtemp(), "--no-compare", "--no-png"], capture_output=True, text=True)
check("an unsafe --key is refused", out.returncode != 0 and "--key must be" in (out.stderr + out.stdout), out.stderr[-300:])

proj = tempfile.mkdtemp()
open(os.path.join(proj, "lakefile.lean"), "w").write('import Lake\nopen Lake DSL\npackage Demo where\n  srcDir := "a/../../../b"\nlean_lib Demo\n')
out = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), proj, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png"],
                     capture_output=True, text=True)
check("a lakefile srcDir outside the project is refused", out.returncode != 0 and "refused" in (out.stderr + out.stdout), out.stderr[-300:])
out = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), tempfile.mkdtemp(), "--main", "../../etc/passwd",
                      "--out", tempfile.mkdtemp(), "--no-compare", "--no-png"], capture_output=True, text=True)
check("a --main outside the project is refused", out.returncode != 0 and "refused" in (out.stderr + out.stdout), out.stderr[-300:])
out = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), "https://github.com/example/x", "--rev=--orphan=x",
                      "--out", tempfile.mkdtemp(), "--no-compare", "--no-png"], capture_output=True, text=True)
check("a --rev that looks like an option is refused", out.returncode != 0 and "--rev must be" in (out.stderr + out.stdout), out.stderr[-300:])

# A card for one of the reference projects, saved under another name, is not compared with that project's reference copy.
import json
proj = tempfile.mkdtemp()
open(os.path.join(proj, "Demo.lean"), "w", encoding="utf-8").write(
    "import Mathlib\ntheorem t1 (x : ℝ) : 0 ≤ Real.sqrt x := Real.sqrt_nonneg x\n"
    "theorem t2 (s : Finset ℕ) : s.card = s.card := Finset.card_eq_of_bijective (fun i _ => i) (by simp) (by simp) (by simp)\n")
def closest(*extra):
    o = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), proj, "--key", "mine", "--out", o, "--no-png", *extra],
                       capture_output=True, text=True)
    k = json.load(open(os.path.join(o, "mine", "card.json"), encoding="utf-8"))
    return r.stdout, [c["key"] for c in (k.get("resemblance") or {}).get("closest", [])]
out1, keys1 = closest("--url", "https://github.com/teorth/PFR.git")
out2, keys2 = closest()
check("a card from a reference project's repository leaves that reference copy out of the comparison",
      "pfr" not in keys1 and len(keys1) == 6 and "same project (pfr)" in out1, (keys1, out1[-300:]))
check("other cards are compared with all seven reference projects", "pfr" in keys2 and len(keys2) == 7, keys2)
# Erdős 846 is one file in a collection: another file from the same repository is a different project; the same file is not.
erdos = "https://github.com/google-deepmind/alphaproof-nexus-results"
os.makedirs(os.path.join(proj, "APNOutputs", "ErdosProblems"), exist_ok=True)
for fn in ("erdos_846.lean", "erdos_1.lean"):
    open(os.path.join(proj, "APNOutputs", "ErdosProblems", fn), "w", encoding="utf-8").write(open(os.path.join(proj, "Demo.lean"), encoding="utf-8").read())
_, keys3 = closest("--url", erdos, "--include", "APNOutputs/ErdosProblems/erdos_1.lean")
_, keys4 = closest("--url", erdos, "--include", "APNOutputs/ErdosProblems/erdos_846.lean")
check("another problem from the same collection is still compared with Erdős 846", "erdos846" in keys3 and len(keys3) == 7, keys3)
check("the same file from that collection is not compared with its own reference copy", "erdos846" not in keys4 and len(keys4) == 6, keys4)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
