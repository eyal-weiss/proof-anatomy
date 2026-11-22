#!/usr/bin/env python3
"""Security regression tests: each one replays an attack found in the pre-release security review.
Run: python3 tests/test_security.py   (needs git; uses the Mathlib cache like the other tests)"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "proof_anatomy"))
sys.argv = ["anatomy.py"]
import anatomy as A
import card as CARD
import extract as X
import mathlib_data as D

FAILS = []

def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: FAILS.append(name)

def run(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), *args], capture_output=True, text=True)

def project(files):
    d = tempfile.mkdtemp()
    for fn, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, fn)) or d, exist_ok=True)
        open(os.path.join(d, fn), "w", encoding="utf-8").write(text)
    return d

def everything(folder):
    out = ""
    for dp, _, fs in os.walk(folder):
        for f in fs:
            if f.endswith((".json", ".html")): out += open(os.path.join(dp, f), encoding="utf-8", errors="replace").read()
    return out

DEMO = {"Demo.lean": "theorem demo_thm : True := trivial\n"}

# 1. A repository's git config cannot start a program while its history is read (promisor remote + core.sshCommand).
g = tempfile.mkdtemp(); env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b.c",
                               "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b.c"}
subprocess.run(["git", "init", "-q", g], check=True)
for i in range(3): subprocess.run(["git", "-C", g, "commit", "-q", "--allow-empty", "-m", str(i)], check=True, env=env)
p = subprocess.run(["git", "-C", g, "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
os.remove(os.path.join(g, ".git", "objects", p[:2], p[2:]))
marker = os.path.join(g, "PWNED")
for k, v in [("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
             ("remote.origin.url", "ssh://example.invalid/repo"), ("remote.origin.promisor", "true"),
             ("core.sshCommand", f"sh -c 'touch {marker}; exit 1' --")]:
    subprocess.run(["git", "-C", g, "config", k, v], check=True)
CARD.git_history(g)
A.git("rev-parse", "HEAD", cwd=g, capture=True, offline=True)
check("git config in the project cannot run a command while history is read", not os.path.exists(marker))

# 2. The Mathlib index is plain data (a pickle can run code when loaded).
check("the Mathlib index is not a pickle", not hasattr(D, "pickle") and D.INDEX.endswith(".json.gz"))

# 3. Author fields with carriage returns cannot forge history rows, and history strings are escaped.
rows = CARD.parse_history("2020-01-01\tA\tx\r2020-01-<img src=x onerror=alert(1) \tA\tB\r2020-04-01\tA\tB\rx\n"
                          "2020-02-03\tB\tb@c\n")
check("forged history rows are dropped", [r[0] for r in rows] == ["2020-01-01", "2020-02-03"], rows)
tmp = tempfile.mkdtemp()
K = json.load(open(os.path.join(HERE, "reference", "cards", "pfr", "card.json"), encoding="utf-8"))
K["history"]["first"] = "<i>INJECTED</i>"; K["history"]["per_month"][0][0] = "<i>INJECTED</i>"
json.dump(K, open(os.path.join(tmp, "card.json"), "w", encoding="utf-8"))
CARD.render(tmp, png=False)
check("history strings are escaped in the card", "<i>INJECTED</i>" not in open(os.path.join(tmp, "card.html"), encoding="utf-8").read())

# 4. A symbolic link named lean-toolchain cannot copy a private file into the card.
secret = tempfile.mkdtemp(); open(os.path.join(secret, "credentials"), "w").write("aws_secret_access_key=AKIAFAKESECRET\n")
pr = project(DEMO); os.symlink(os.path.join(secret, "credentials"), os.path.join(pr, "lean-toolchain"))
os.symlink(os.path.join(secret, "credentials"), os.path.join(pr, "lake-manifest.json"))
out = tempfile.mkdtemp(); r = run(pr, "--out", out, "--no-compare", "--no-png", "--no-history")
check("a local folder with a symbolic link is refused, and nothing is read", r.returncode != 0 and "refused" in r.stderr
      and "AKIAFAKESECRET" not in everything(out) + r.stdout, r.stderr[-300:])

# 5. Lakefile entries cannot inject options into the extraction step.
sec = project({"Secret.lean": "theorem secret_thm : True := trivial\n"})
pr = project({**DEMO, "lakefile.toml": f'name = "demo"\n[[lean_lib]]\nname = "Demo"\nroots = ["--root", "{sec}"]\n'})
out = tempfile.mkdtemp(); r = run(pr, "--out", out, "--no-compare", "--no-png", "--no-history")
check("a lakefile entry such as --root is refused", r.returncode != 0 and "refused" in r.stderr
      and "secret_thm" not in everything(out) + r.stdout, r.stdout[-400:] + r.stderr[-300:])

# 6. Outputs are never written through a symbolic link.
victim = os.path.join(tempfile.mkdtemp(), "config"); open(victim, "w").write("KEEP\n")
out = tempfile.mkdtemp(); os.makedirs(os.path.join(out, "demo"))
os.symlink(victim, os.path.join(out, "demo", "card.json"))
r = run(project(DEMO), "--key", "demo", "--out", out, "--no-compare", "--no-png", "--no-history")
check("a planted output link is refused", r.returncode != 0 and "symbolic link" in r.stderr + r.stdout
      and open(victim).read() == "KEEP\n", r.stderr[-300:])
out = tempfile.mkdtemp(); os.symlink(tempfile.mkdtemp(), os.path.join(out, "demo"))
r = run(project(DEMO), "--key", "demo", "--out", out, "--no-compare", "--no-png", "--no-history")
check("a linked card folder is refused", r.returncode != 0 and "symbolic link" in r.stderr + r.stdout, r.stderr[-300:])

# 7. Credentials in the source URL never reach the card or the screen.
check("user name, token, query and fragment are removed from a URL",
      A.public_url("https://alice:ghp_SECRET@github.com/acme/proof.git?token=x#frag") == "https://github.com/acme/proof.git")
check("an ordinary URL is unchanged", A.public_url("https://github.com/teorth/pfr") == "https://github.com/teorth/pfr")

# 8. Thousands of nested namespaces cannot exhaust memory.
d = tempfile.mkdtemp()
open(os.path.join(d, "Deep.lean"), "w").write("namespace N\n" * 4000 + "theorem t : True := sorry\n" + "end N\n" * 4000)
X._init_worker(frozenset()); t0 = time.time()
rec, names = X.scan((d, "Deep.lean")), X.decl_names((d, "Deep.lean"))
check("a file with 4000 nested namespaces is skipped quickly", rec is None and names == [] and time.time() - t0 < 30,
      f"{time.time() - t0:.1f}s")
open(os.path.join(d, "Interp.lean"), "w").write('def x := ' + 's!"{' * 5000 + "1" + '}"' * 5000 + "\n")
check("thousands of nested interpolated strings are skipped, not a crash",
      X.scan((d, "Interp.lean")) is None and X.decl_names((d, "Interp.lean")) == [])
ok = project({"Ok.lean": "namespace A\nnamespace B\ntheorem t : True := trivial\nend B\nend A\n"})
check("ordinary nesting still works", X.decl_names((ok, "Ok.lean")) == ["A.B.t"], X.decl_names((ok, "Ok.lean")))

# 9. A .lean path that is not a regular file (a FIFO here) is skipped instead of blocking the run.
pr = project(DEMO)
if hasattr(os, "mkfifo"):
    os.mkfifo(os.path.join(pr, "Blocker.lean"))
    out = tempfile.mkdtemp()
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "anatomy.py"), pr, "--out", out, "--no-compare", "--no-png",
                            "--no-history"], capture_output=True, text=True, timeout=120)
        check("a FIFO named .lean is skipped", r.returncode == 0 and "extract: 1 files" in r.stdout, r.stdout[-300:])
    except subprocess.TimeoutExpired:
        check("a FIFO named .lean is skipped", False, "the run blocked")

# --- second review -------------------------------------------------------------------------------------------
# 10. A carriage return in an author field cannot forge a row, and an impossible date cannot start an endless loop.
g2 = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", g2], check=True)
evil = {**env, "GIT_AUTHOR_EMAIL": "x\r2020-99-01\tA\tB\r2021-01-01\tA\tB\rx"}
for i in range(25):
    subprocess.run(["git", "-C", g2, "commit", "-q", "--allow-empty", "-m", str(i), "--date", f"2021-0{1 + i % 6}-15T12:00:00"],
                   check=True, env=evil if i == 0 else {**env, "GIT_AUTHOR_DATE": f"2021-0{1 + i % 6}-15T12:00:00"})
t0 = time.time(); h = CARD.git_history(g2)
check("forged history dates cannot loop forever", time.time() - t0 < 30 and (h is None or h["first"] >= "2021"), h and h["first"])
check("an impossible date is rejected", CARD.parse_history("2020-99-01\tA\tB\n") == [])

# 11. One namespace with 100,000 dotted components is skipped without building its prefixes.
open(os.path.join(d, "Dotted.lean"), "w").write("namespace " + ".".join(["N"] * 100000) + "\ntheorem t : True := sorry\n")
t0 = time.time()
check("a namespace with 100,000 components is skipped quickly", X.scan((d, "Dotted.lean")) is None and time.time() - t0 < 10,
      f"{time.time() - t0:.1f}s")

# 12. Many `open … in` prefixes before a non-declaration do not make the declaration pattern backtrack for ever.
t0 = time.time(); X.DECL.match("open Nat in " * 60 + "#check True")
check("60 `open … in` prefixes are matched in linear time", time.time() - t0 < 1, f"{time.time() - t0:.1f}s")
check("`open … in` before a theorem is still recognized",
      X.DECL.match("open Nat in open Real in theorem foo : True := trivial").group(3) == "foo")

t0 = time.time(); open(os.path.join(d, "Dots.lean"), "w").write("theorem t : " + "a." * 500000 + "b := sorry\n")
X.scan((d, "Dots.lean"))
check("a dotted name with 500,000 parts is read in seconds", time.time() - t0 < 20, f"{time.time() - t0:.1f}s")

# 13. Credentials in the source URL do not reach the default title or folder name.
for src, want in (("https://alice:SECRET@git.example/proof.git", "proof"), ("https://git.example/proof.git?token=SECRET", "proof"),
                  ("https://alice:SECRET@git.example/", "project"), ("git@github.com:owner/repo.git", "repo"),
                  ("https://github.com/teorth/pfr", "pfr")):
    check(f"default name of {src[:40]} is {want}", A.default_name(src, True) == want, A.default_name(src, True))

# 14. A symbolic link above the output folder, inside a local project, is refused.
pr = project(DEMO); elsewhere = tempfile.mkdtemp(); os.makedirs(os.path.join(elsewhere, "demo"))
os.symlink(elsewhere, os.path.join(pr, "public"))
r = run(pr, "--key", "demo", "--out", os.path.join(pr, "public"), "--no-compare", "--no-png", "--no-history")
check("an output folder inside the project is refused", r.returncode != 0 and "refused" in r.stderr + r.stdout
      and not os.listdir(os.path.join(elsewhere, "demo")), r.stderr[-300:])
out_ok = tempfile.mkdtemp(); r = run(project(DEMO), "--key", "demo", "--out", out_ok, "--no-compare", "--no-png", "--no-history")
check("an ordinary output folder still works", r.returncode == 0 and os.path.exists(os.path.join(out_ok, "demo", "card.json")), r.stderr[-300:])

# --- strict input policy -----------------------------------------------------------------------------------
# 15. Only plain public https:// links are accepted.
for src in ("git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo", "file:///etc", "ext::sh -c id",
            "https://alice:SECRET@github.com/owner/repo", "https://github.com/owner/repo?token=SECRET", "http://github.com/o/r"):
    r = run(src, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png")
    check(f"source refused: {src[:40]}", r.returncode != 0 and "refused" in r.stderr and "SECRET" not in r.stdout + r.stderr, r.stderr[-200:])

# 16. Git is never run on a local folder: the promisor-remote config from test 1 does nothing, and there is no history.
pr = project(DEMO); shutil.copytree(os.path.join(g, ".git"), os.path.join(pr, ".git"))
cfg = os.path.join(pr, ".git", "config"); marker2 = os.path.join(pr, "PWNED2")
open(cfg, "a").write(f"[core]\n\tsshCommand = sh -c 'touch {marker2}; exit 1' --\n\tfsmonitor = sh -c 'touch {marker2}'\n")
out = tempfile.mkdtemp(); r = run(pr, "--key", "demo", "--out", out, "--no-compare", "--no-png")
K2 = json.load(open(os.path.join(out, "demo", "card.json"))) if r.returncode == 0 else {}
check("git is not run on a local folder", r.returncode == 0 and not os.path.exists(marker2) and not K2.get("history"), r.stderr[-300:])

# 17. Options that are not plain names or paths are refused.
for args in (["--main", "../x.lean"], ["--main", "/etc/x.lean"], ["--main", "Main.txt"], ["--decl", "$(id)"],
             ["--decl", "a b"], ["--include", "../other"], ["--src-dir", "/home"]):
    r = run(project(DEMO), "--out", tempfile.mkdtemp(), "--no-compare", "--no-png", *args)
    check(f"option refused: {' '.join(args)}", r.returncode != 0 and "refused" in r.stderr, r.stderr[-200:])
r = run(project({"Main.lean": "theorem main_thm : True := trivial\n"}), "--out", tempfile.mkdtemp(), "--no-compare", "--no-png",
        "--main", "Main.lean", "--decl", "main_thm")
check("plain options still work", r.returncode == 0, r.stderr[-300:])

# --- third review: blanket limits --------------------------------------------------------------------------
# 18. Links: allowed hosts only, nothing unusual.
for src in ("https://example.com/o/r", "https://github.com/o/r?", "https://github.com/o/r#", "https://github.com:443/o/r",
            "https://github.com/o/r\n", "https://github.com/o/r%2e", "HTTPS://github.com/o/r", "https://www.github.com/o/r"):
    r = run(src, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png")
    check(f"link refused: {src!r}", r.returncode != 0 and "refused" in r.stderr, r.stderr[-200:])
for u in ("https://alice:SECRET@git.example/p", "HTTPS://alice:SECRET@git.example/p", "//alice:SECRET@git.example/p", "x?token=1"):
    r = run(project(DEMO), "--out", tempfile.mkdtemp(), "--no-compare", "--no-png", "--url", u)
    check(f"--url refused: {u[:30]}", r.returncode != 0 and "refused" in r.stderr, r.stderr[-200:])

# 19. Names that would expand enormously are refused per file, before the expansion.
bombs = {"Alias.lean": "open " + "N" * 100000 + " (" + " ".join(f"x{i}" for i in range(25000)) + ")\n",
         "LongNs.lean": "namespace " + "N" * 5000 + "\ntheorem t : True := trivial\n",
         "ManyDecls.lean": "namespace " + "N" * 4000 + "\n" + "".join(f"theorem t{i} : True := trivial\n" for i in range(20000))}
for fn, text in bombs.items():
    open(os.path.join(d, fn), "w").write(text); t0 = time.time()
    check(f"{fn} is skipped quickly", X.scan((d, fn)) is None and time.time() - t0 < 30, f"{time.time() - t0:.1f}s")

# 20. The limits apply to the folder actually read (here srcDir = "build", a folder the tool otherwise skips).
pr = project({"lakefile.toml": 'name = "demo"\nsrcDir = "build"\n[[lean_lib]]\nname = "Demo"\n'})
os.makedirs(os.path.join(pr, "build")); open(os.path.join(pr, "build", "Demo.lean"), "w").write(DEMO["Demo.lean"])
os.symlink(os.path.join(secret, "credentials"), os.path.join(pr, "build", "Leak.lean"))
r = run(pr, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png")
check("a link inside the folder that is read is refused, even under build/", r.returncode != 0 and "refused" in r.stderr, r.stderr[-200:])

# 21. A main file with thousands of local names is traced without keeping a copy of them for every line.
body = "def big : Nat → Nat := by\n" + "".join(f"  intro x{i}\n" for i in range(3000)) + "  exact 0\n"
pr = project({"Main.lean": "theorem main_thm : True := trivial\n" + body})
t0 = time.time(); r = run(pr, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png", "--main", "Main.lean", "--decl", "main_thm")
check("a main file with 3000 intros is handled quickly", r.returncode == 0 and time.time() - t0 < 60, f"{time.time() - t0:.0f}s {r.stderr[-200:]}")

# 22. Child processes get a hard memory limit on Linux (here 1 GB: allocating 2 GB fails at once).
if sys.platform.startswith("linux"):
    r = subprocess.run([sys.executable, "-c", "bytearray(2**31)"], preexec_fn=A.limits(1), capture_output=True, text=True)
    check("the memory limit stops a child that asks for too much", r.returncode != 0 and "MemoryError" in r.stderr, r.stderr[-200:])

# 23. The picture still works under the limits (skipped when no Chrome/Chromium is installed).
if CARD.find_chrome():
    out = tempfile.mkdtemp(); r = run(project(DEMO), "--key", "demo", "--out", out, "--no-compare")
    check("card.png is still made under the memory limit", r.returncode == 0 and os.path.exists(os.path.join(out, "demo", "card.png")), r.stdout[-300:])

# --- fourth review ---------------------------------------------------------------------------------------
# 24. A nested time limit keeps the outer deadline (it used to cancel it).
if hasattr(__import__("signal"), "SIGALRM"):
    X.FILE_SECONDS, t0 = 1, time.time()
    def outer():
        X.limited(lambda: None)                    # an inner limit, as when tracing calls the file parser
        while time.time() - t0 < 10: pass          # then slow work that the outer limit must still stop
    try:
        X.limited(outer); stopped = False
    except X.TooComplex:
        stopped = True
    X.FILE_SECONDS = 120
    check("a nested time limit keeps the outer deadline", stopped and time.time() - t0 < 5, f"{time.time() - t0:.1f}s")

# 25. A lakefile far larger than any real one is refused at once.
pr = project({**DEMO, "lakefile.toml": 'name = "x"\n[[lean_lib]]\nname = "Demo"\n' + "roots = [\n" * 20000})
t0 = time.time(); r = run(pr, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png")
check("an oversized lakefile is refused quickly", r.returncode != 0 and "refused" in r.stderr and time.time() - t0 < 30, r.stderr[-200:])

# 26. A step that writes too much to disk, or runs too long, is stopped (as cloning is watched).
A.CLONE_MAX_BYTES, watch = 5 * 2**20, tempfile.mkdtemp()
writer = ("import os,time,sys\nfor i in range(100):\n open(os.path.join(sys.argv[1], str(i)), 'wb').write(b'x' * 2**20)\n"
          " time.sleep(0.05)\n")
t0 = time.time()
try:
    A.run_limited([sys.executable, "-c", writer, watch], "test", 120, 2, watch=watch); stopped = False
except SystemExit:
    stopped = True
check("a step that fills the disk is stopped", stopped and len(os.listdir(watch)) < 100, f"{len(os.listdir(watch))} files")
A.CLONE_MAX_BYTES = 5 * 2**30
t0 = time.time()
try:
    A.run_limited([sys.executable, "-c", "import time; time.sleep(60)"], "test", 2, 2); stopped = False
except SystemExit:
    stopped = True
check("a step past its deadline is stopped", stopped and time.time() - t0 < 10, f"{time.time() - t0:.1f}s")

# --- fifth review ----------------------------------------------------------------------------------------
# 27. The disk check also runs when a step ends between two checks, and stops counting as soon as a limit is passed.
A.CLONE_MAX_BYTES, watch = 5 * 2**20, tempfile.mkdtemp()
fast = "import os,sys\nfor i in range(10): open(os.path.join(sys.argv[1], str(i)), 'wb').write(b'x' * 2**20)\n"
try:
    A.run_limited([sys.executable, "-c", fast, watch], "test", 120, 2, watch=watch); stopped = False
except SystemExit:
    stopped = True
check("a step that writes too much and ends at once is still refused", stopped)
A.CLONE_MAX_BYTES = 5 * 2**30
many = tempfile.mkdtemp()
for i in range(2000): open(os.path.join(many, str(i)), "w").close()
check("the disk count stops early", A.disk_use(many, 10**12, 100)[0] <= 101, A.disk_use(many, 10**12, 100))

# 28. The folder walk has an entry limit, and main-file candidates come from that same walk.
A.MAX_ENTRIES = 100
try:
    A.survey(many, local=True); stopped = False
except SystemExit:
    stopped = True
A.MAX_ENTRIES = 2_000_000
check("a folder with too many entries is refused", stopped)
r = run(project({"Main.lean": "theorem t : True := trivial\n"}), "--out", tempfile.mkdtemp(), "--no-compare", "--no-png",
        "--main", "Missing.lean")
check("a missing --main lists the Main files found", r.returncode != 0 and "Main.lean" in r.stderr, r.stderr[-200:])

# --- sixth review ----------------------------------------------------------------------------------------
# 29. Folders nested deeper than MAX_DEPTH are refused (os.walk would recurse past Python's limit).
pr = project(DEMO); deep = pr
for i in range(70): deep = os.path.join(deep, "d")
os.makedirs(deep); open(os.path.join(deep, "X.lean"), "w").write("theorem x : True := trivial\n")
r = run(pr, "--out", tempfile.mkdtemp(), "--no-compare", "--no-png")
check("folders nested 70 levels deep are refused", r.returncode != 0 and "refused" in r.stderr, r.stderr[-200:])

# 30. If the supervision itself fails, the child is still stopped.
pidfile = os.path.join(tempfile.mkdtemp(), "pid")
child = f"import os,time; open({pidfile!r},'w').write(str(os.getpid())); time.sleep(60)"
real_disk_use = A.disk_use
def broken(*a):
    if os.path.exists(pidfile): raise RuntimeError("monitor failed")
    return (0, 0)
A.disk_use = broken
try:
    A.run_limited([sys.executable, "-c", child], "test", 120, 2, watch=tempfile.mkdtemp())
except RuntimeError:
    pass
A.disk_use = real_disk_use
pid = int(open(pidfile).read()) if os.path.exists(pidfile) else None
alive = True
try:
    os.kill(pid, 0)
except (ProcessLookupError, TypeError):
    alive = False
check("a failure in the watchdog still stops the child", pid is not None and not alive)

# 31. Field names under a long namespace are charged like declaration names (they used to inflate the records).
open(os.path.join(d, "Fields.lean"), "w").write("namespace " + "N" * 4000 + "\nstructure S where\n"
                                                  + "".join(f"  f{i} : Nat\n" for i in range(3000)))
t0 = time.time()
check("thousands of fields under a long namespace are skipped", X.scan((d, "Fields.lean")) is None and time.time() - t0 < 30)

# --- seventh review --------------------------------------------------------------------------------------
# 32. The tool picks the files itself: regular .lean files and root metadata only, every path counted.
o = lambda c: c * 40
listing = "\x00".join([f"100644 blob {o('a')}    1000\tA.lean", f"100644 blob {o('a')}    1000\tB/A.lean",
                       f"100644 blob {o('b')}  999999\tbig.pdf", f"100644 blob {o('c')}      50\tlakefile.toml",
                       f"040000 tree {o('d')}       -\tB", f"100644 blob {o('e')} 7\tsub/lakefile.toml",
                       f"100644 blob {o('f')} 1048576\tPayload.lean/00001.bin", f"100644 blob {o('f')} 1048576\tlakefile.toml/x",
                       f"120000 blob {o('a')} 20\tLink.lean", f"160000 commit {o('a')} -\tSub.lean", f"100644 blob {o('a')} 1000\t.hidden/C.lean", ""])
sel = A.select_files(listing)
check("only regular .lean files and root metadata are selected", [p for p, _, _ in sel] == ["A.lean", "B/A.lean", "lakefile.toml"]
      and sum(s for _, _, s in sel) == 2050, sel)
try:
    A.select_files(f"100644 blob {o('a')} 5\tA/../B.lean\x00"); refused_dots = False
except SystemExit:
    refused_dots = True
check("a path with .. is refused", refused_dots)

# 32b. Files are written as stored: a .gitattributes asking for CRLF and UTF-32 changes nothing.
rg = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", rg], check=True)
open(os.path.join(rg, "A.lean"), "w").write("a\nb\n")
subprocess.run(["git", "-C", rg, "add", "A.lean"], check=True, env=env)         # stored as plain "a\nb\n"
open(os.path.join(rg, ".gitattributes"), "w").write("*.lean text eol=crlf working-tree-encoding=UTF-32LE\n")
subprocess.run(["git", "-C", rg, "add", ".gitattributes"], check=True, env=env)
subprocess.run(["git", "-C", rg, "commit", "-q", "-m", "x"], check=True, env=env, capture_output=True)
head = subprocess.run(["git", "-C", rg, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
files = A.select_files(subprocess.run(["git", "-C", rg, "ls-tree", "-r", "-z", "-l", head], capture_output=True, text=True).stdout)
dest = os.path.join(tempfile.mkdtemp(), "repo"); os.makedirs(dest)
A.write_blobs(rg, files, dest, time.time() + 60)
check("files are written byte for byte as stored", open(os.path.join(dest, "A.lean"), "rb").read() == b"a\nb\n"
      and sorted(os.listdir(dest)) == ["A.lean"])

# 33. Cleanup stops the whole process group, also when the group leader has already exited.
pidfile = os.path.join(tempfile.mkdtemp(), "pid")
leader = ("import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
          f"\"import os,time; open({pidfile!r},'w').write(str(os.getpid())); time.sleep(60)\"])")
calls = [0]
def broken2(*a):
    calls[0] += 1
    if os.path.exists(pidfile): raise RuntimeError("monitor failed")
    return (0, 0)
A.disk_use = broken2
try:
    A.run_limited([sys.executable, "-c", leader + "; import time; time.sleep(1)"], "test", 120, 2, watch=tempfile.mkdtemp())
except RuntimeError:
    pass
A.disk_use = real_disk_use
time.sleep(0.5)
pid = int(open(pidfile).read()) if os.path.exists(pidfile) else None
alive = True
try:
    os.kill(pid, 0)
    st = open(f"/proc/{pid}/stat").read().split()[2] if os.path.exists(f"/proc/{pid}/stat") else "R"
    alive = st != "Z"
except (ProcessLookupError, TypeError):
    alive = False
check("a helper process is stopped even after the leader exited", pid is not None and not alive)

# 34. A disk scan that takes too long counts as over the limit instead of holding up the deadline.
slow_end = time.time
real_time = A.time.time
seq = iter([0.0] + [100.0] * 100000)
A.time.time = lambda: next(seq)
try:
    res = A.disk_use(many, 10**12, 10**9)
finally:
    A.time.time = real_time
check("a slow disk scan is treated as over the limit", res[0] > 10**9, res)

# --- ninth review ----------------------------------------------------------------------------------------
# 35. Folders are counted before anything is written: many files each under its own deep chain of folders are refused.
deep_files = [(f"p{i:06d}/" + "d/" * 60 + "X.lean", "a" * 40, 0) for i in range(2000)]
try:
    A.check_layout(deep_files); refused_dirs = False
except SystemExit:
    refused_dirs = True
check("2,000 files under separate 60-level folder chains (122,000 folders) are refused", refused_dirs)
ok_files = [(f"Proj/Sub{i % 50}/F{i}.lean", "a" * 40, 3000) for i in range(5000)]
check("an ordinary layout passes, with folders and block rounding counted", A.check_layout(ok_files) == (51, 5000 * 2 * 4096 + 52 * 4096),
      A.check_layout(ok_files))

# --- tenth review ----------------------------------------------------------------------------------------
# 36. Git's own data counts toward the clone's 5 GB: 3 GB of git data plus 2.5 GB of Lean files is refused.
big_files = [(f"F{i}.lean", "a" * 40, 16 * 2**20) for i in range(160)]          # 2.5 GB, under the 4 GB materialization limit
try:
    A.check_layout(big_files, used_bytes=3 * 2**30); refused_total = False
except SystemExit:
    refused_total = True
check("git data plus Lean files over 5 GB is refused", refused_total)
check("the same files fit when git's data is small", A.check_layout(big_files, used_bytes=50 * 2**20)[0] == 0)

# --- cloud test (2026-11-22) ------------------------------------------------------------------------------
# 37. When Mathlib's sites cannot be reached (a sandbox that allows only GitHub and package sites) or send something
#     that is not the real file, the copy included with the tool is used and marked; a real download is never replaced
#     by it; and a later run upgrades the included copy when the download works again.
saved_urls, saved_env = dict(D.URLS), os.environ.get("PROOF_ANATOMY_DATA")
cache = tempfile.mkdtemp(); os.environ["PROOF_ANATOMY_DATA"] = cache
block = os.path.join(tempfile.mkdtemp(), "blocked.html"); open(block, "w").write("<html>Host not in allowlist</html>")
try:
    D.URLS.update({"declarations": "https://invalid.invalid/declaration-data.bmp", "1000.yaml": "file://" + block,
                   "100.yaml": "https://invalid.invalid/100.yaml"})
    _, meta = D.ensure(quiet=True, retry=True)
    T = D.load_index()
    same = all(open(os.path.join(cache, f), "rb").read() == open(os.path.join(D.BUNDLED, f), "rb").read()
               for f in D.FILES.values())
    check("unreachable or wrong downloads fall back to the included copy, marked as such",
          same and all(meta[i].get("bundled") and meta[i]["fetched"] for i in D.FILES) and len(T["names"]) > 400000, meta)
    D.URLS["declarations"] = "file://" + block
    _, meta = D.ensure(quiet=True, retry=True)
    check("a block page instead of the declaration table keeps the included copy", meta["declarations"].get("bundled"))
    meta["declarations"] = {"url": "x", "fetched": "2020-01-01"}                       # pretend a real download is cached
    json.dump(meta, open(os.path.join(cache, "fetched.json"), "w"))
    idx_bytes = open(os.path.join(cache, D.INDEX), "rb").read()
    _, meta = D.ensure(quiet=True, refresh=True)
    check("a failed refresh keeps a real cached download", meta["declarations"] == {"url": "x", "fetched": "2020-01-01"}
          and open(os.path.join(cache, D.INDEX), "rb").read() == idx_bytes, meta)
    meta["declarations"] = {"url": "x", "fetched": "2020-01-01", "bundled": True}
    json.dump(meta, open(os.path.join(cache, "fetched.json"), "w"))
    fake = os.path.join(tempfile.mkdtemp(), "decl.json")
    json.dump({"declarations": {f"N.d{i}": {"docLink": "./Mathlib/A/B.html#x", "kind": "theorem"} for i in range(100001)}},
              open(fake, "w"))
    D.URLS["declarations"] = "file://" + fake
    _, meta = D.ensure(quiet=True)
    check("without retry (the tool's worker processes) the included copy is not downloaded again",
          meta["declarations"].get("bundled"))
    _, meta = D.ensure(quiet=True, retry=True)
    check("with retry, a working download replaces the included copy",
          not meta["declarations"].get("bundled") and len(D.load_index()["names"]) == 100001, meta["declarations"])
    # Codex review 12: a damaged fetched.json must not cost a real download, and one bad record must not cost the others.
    real = open(os.path.join(cache, D.INDEX), "rb").read()
    D.URLS["declarations"] = "https://invalid.invalid/declaration-data.bmp"
    open(os.path.join(cache, "fetched.json"), "w").write('{"declarations": {"url": "x", "fetch')
    D.ensure(quiet=True, retry=True)
    check("a damaged fetched.json keeps the downloaded files", open(os.path.join(cache, D.INDEX), "rb").read() == real)
    json.dump({"declarations": "junk", "100.yaml": {"url": "y", "fetched": "2021-01-01"}}, open(os.path.join(cache, "fetched.json"), "w"))
    check("one damaged record leaves the others", D.fetched() == {"100.yaml": {"url": "y", "fetched": "2021-01-01"}}, D.fetched())
    check("no partial files are left in the cache", not [f for f in os.listdir(cache) if f.endswith(".part")], os.listdir(cache))
    # Two runs at once: a run whose download fails waits for the other and then keeps what that run installed.
    import threading
    cache2 = tempfile.mkdtemp(); os.environ["PROOF_ANATOMY_DATA"] = cache2
    result = {}
    with D._Lock(cache2):                                                             # the other run is updating
        th = threading.Thread(target=lambda: result.update(meta=D.ensure(quiet=True, retry=True)[1])); th.start()
        time.sleep(1.0); waited = th.is_alive()
        for f in D.FILES.values(): shutil.copyfile(os.path.join(D.BUNDLED, f), os.path.join(cache2, f))
        json.dump({i: {"url": "real", "fetched": "2026-01-01"} for i in D.FILES}, open(os.path.join(cache2, "fetched.json"), "w"))
    th.join(60)
    check("a concurrent run waits, then keeps the other run's download instead of the included copy",
          waited and result.get("meta", {}).get("declarations") == {"url": "real", "fetched": "2026-01-01"}, result)
    shutil.rmtree(cache2, ignore_errors=True)
    # Codex review 13: on Windows, waiting is only for another run holding the lock; other errors stop the run.
    import errno, types
    calls = []
    def fake_locking(fd, mode, n):
        calls.append(mode)
        if fake.fail == "busy" and len(calls) <= 2: raise OSError(errno.EACCES, "busy")
        if fake.fail == "broken" and mode == fake.LK_NBLCK: raise OSError(errno.EIO, "drive gone")
    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=fake_locking, fail="busy")
    saved_name, saved_mod = os.name, sys.modules.get("msvcrt")
    sys.modules["msvcrt"] = fake; os.name = "nt"
    try:
        with D._Lock(cache):
            pass
        waited_busy = calls[:3] == [2, 2, 2] and calls[-1] == 0
        calls.clear(); fake.fail = "broken"; t0 = time.time()
        try:
            D._Lock(cache); stopped = False
        except SystemExit:
            stopped = True
    finally:
        os.name = saved_name
        if saved_mod is None: sys.modules.pop("msvcrt", None)
        else: sys.modules["msvcrt"] = saved_mod
    check("Windows: the lock waits while another run holds it, then is released", waited_busy, calls)
    check("Windows: a locking error that is not contention stops at once with a message",
          stopped and time.time() - t0 < 5 and len(calls) == 1, calls)
finally:
    D.URLS.clear(); D.URLS.update(saved_urls)
    if saved_env is None: os.environ.pop("PROOF_ANATOMY_DATA", None)
    else: os.environ["PROOF_ANATOMY_DATA"] = saved_env
    shutil.rmtree(cache, ignore_errors=True)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
