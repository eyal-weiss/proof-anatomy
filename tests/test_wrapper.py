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

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
