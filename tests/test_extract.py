#!/usr/bin/env python3
"""Regression tests for the text scanner and name lookup. Run: python3 tests/test_extract.py
(needs Mathlib's table, downloaded on first use; about 3 seconds to load)."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proof_anatomy"))
import extract as X

FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: FAILS.append(name)


def decl_names(src):
    d, aux, _, _, _, _, _ = X.walk(X.X_lines(src))
    return [f for _, _, _, f in d], aux


def use(src, tok, own=()):
    """How `tok` resolves on the last line of src, with `own` as the project's declarations."""
    X.OWN = frozenset(own)
    F = X.file_model(src)
    li = len(F["lines"]) - 1
    while not F["lines"][li].strip(): li -= 1
    k = max([i for i, (dl, _, _, _) in enumerate(F["decls"]) if dl <= li] or [-1])
    locs = X.locals_until(F, k, li) if k >= 0 else frozenset()
    owner = F["decls"][k][3] if k >= 0 and F["decls"][k][3] in F["owners"] else None
    r = X.resolve_ctx(tok, F["ctxs"][F["ctx_of"][li]], locs, owner)
    return None if r is None else (r[0], r[1] if r[0] == "own" else X.load_index()["cname"][r[1]])


# ---------------------------------------------------------------- lexing: nothing after a literal is swallowed
for label, src in [("char literal '\"'", "def ch : Char := '\"'\ndef good := 1"),
                   ("raw string r\"\\\"", 'def msg := r"\\"\ndef good := 1'),
                   ("raw string r#\"…\"#", 'def msg := r#"a " /- raw text"#\ndef good := 1'),
                   ("interpolation with a nested string", 'def msg := s!"{"/-"}"\ndef good := 1'),
                   ("escaped quote in a string", 'def msg := "a \\" b"\ndef good := 1'),
                   ("escaped apostrophe char", "def ch : Char := '\\''\ndef good := 1")]:
    names, _ = decl_names(src)
    check(label, names[-1:] == ["good"], names)
check("comment between keyword and name", decl_names("def/-c-/foo := 1")[0] == ["foo"], decl_names("def/-c-/foo := 1"))
src = 'def msg := "line one\nline two"\ndef good := 1'
check("multiline string keeps its line breaks", len(X.X_lines(src)) == 3, len(X.X_lines(src)))
check("code inside s!\"{…}\" is kept", "Nat.succ" in X.strip_comments('def x := s!"{Nat.succ 0}"'), X.strip_comments('def x := s!"{Nat.succ 0}"'))
check("prime notation f '' s is not a char literal", "image" in X.strip_comments("theorem t : f '' s = image f s := rfl"))

# ---------------------------------------------------------------- declarations
check("universe parameters", decl_names("def identity.{u} (x : Sort u) := x")[0] == ["identity"], decl_names("def identity.{u} (x : Sort u) := x"))
check("quoted name", decl_names("def «hello world» := 1")[0] == ["hello world"], decl_names("def «hello world» := 1"))
check("open … in before a declaration", decl_names("open Nat in theorem okay : True := by trivial")[0] == ["okay"],
      decl_names("open Nat in theorem okay : True := by trivial"))
check("where auxiliaries", "wrapper.helper" in decl_names("def wrapper := helper\nwhere\n  helper := 1")[1], decl_names("def wrapper := helper\nwhere\n  helper := 1"))
check("structure fields", {"Foo.x", "Foo.y"} <= set(decl_names("structure Foo where\n  x : Nat\n  y : Nat")[1]),
      decl_names("structure Foo where\n  x : Nat\n  y : Nat"))
check("inductive constructors", {"Color.red", "Color.blue"} <= set(decl_names("inductive Color where\n  | red\n  | blue")[1]),
      decl_names("inductive Color where\n  | red\n  | blue"))
check("nested namespaces", decl_names("namespace A\nnamespace B\ndef f := 1\nend B\nend A")[0] == ["A.B.f"],
      decl_names("namespace A\nnamespace B\ndef f := 1\nend B\nend A"))
lines = X.X_lines("def foo := 1\n  def unrelated := Real.sqrt 1\ntheorem main : foo = 1 := rfl")
check("body ends at an indented declaration", X.body_end(lines, 0, 0) == 1, X.body_end(lines, 0, 0))

# ---------------------------------------------------------------- statements
st = X.statement_of("theorem main (nn : Nat := 0) : Real.sqrt (nn : Real) = Real.sqrt nn := rfl", "theorem")
check("default argument inside the statement", "Real.sqrt nn" in st, st)
st = X.statement_of("theorem main : ∀ x : ℝ, 0 ≤ |x|\n  | x => abs_nonneg x", "theorem")
check("equation-style proof is not part of the statement", "abs_nonneg" not in st, st)
st = X.statement_of("theorem foo (x : ℝ) (h : 0 ≤ x) :\n    |x| = x := abs_of_nonneg h", "theorem")
check("absolute value at the start of a line stays in the statement", "|x| = x" in st, st)

# ---------------------------------------------------------------- name lookup (Lean's order)
X.load_index()
check("current namespace before root", use("namespace A\ndef Real : Prop := True\ntheorem main : Real := by trivial", "Real", ["A.Real"]) == ("own", "A.Real"),
      use("namespace A\ndef Real : Prop := True\ntheorem main : Real := by trivial", "Real", ["A.Real"]))
check("root Real outside the namespace", use("theorem main : Real := sorry", "Real")[0] == "mathlib")
check("open X (a) opens only a", use("open Real (sqrt)\ntheorem t : log 1 = 0 := sorry", "log") is None, use("open Real (sqrt)\ntheorem t : log 1 = 0 := sorry", "log"))
check("open X (a) gives a", use("open Real (sqrt)\ntheorem t : sqrt 1 = 1 := sorry", "sqrt") == ("mathlib", "Real.sqrt"))
check("open X hiding a", use("open Real hiding log\ntheorem t : log 1 = 0 := sorry", "log") is None, use("open Real hiding log\ntheorem t : log 1 = 0 := sorry", "log"))
check("open scoped X opens no names", use("open scoped Real\ntheorem t : log 1 = 0 := sorry", "log") is None)
check("open X renaming a → b", use("open Real renaming sqrt → root\ntheorem t : root 1 = 1 := sorry", "root") == ("mathlib", "Real.sqrt"),
      use("open Real renaming sqrt → root\ntheorem t : root 1 = 1 := sorry", "root"))
check("an open ends with its section", use("section\nopen Real\nend\ntheorem t : log 1 = 0 := sorry", "log") is None,
      use("section\nopen Real\nend\ntheorem t : log 1 = 0 := sorry", "log"))
check("open … in applies to the next command only", use("open Real in\ntheorem a : log 1 = 0 := sorry\ntheorem t : log 1 = 0 := sorry", "log") is None)
check("open … in applies to that command", use("open Real in\ntheorem a : log 1 = 0 := sorry", "log") == ("mathlib", "Real.log"))
check("a local binder shadows a global name", use("theorem main (Continuous : Prop) (h : Continuous) : Continuous := h", "Continuous") is None,
      use("theorem main (Continuous : Prop) (h : Continuous) : Continuous := h", "Continuous"))
check("a variable shadows a global name", use("variable (exp : ℕ)\ntheorem t : exp = exp := rfl", "exp") is None)
check("field access on a constant: A.b.c -> A.b", use("theorem t : True := (Nat.succ_le).mp sorry\ntheorem u := Nat.succ_le.mp", "Nat.succ_le.mp") == ("mathlib", "Nat.succ_le"),
      use("theorem u := Nat.succ_le.mp", "Nat.succ_le.mp"))
check("a bare namespace is not a use", use("theorem u := Real.no_such_lemma_xyz", "Real.no_such_lemma_xyz") is None)
check("field access on a project constant", use("def E8Packing : Nat := 1\ntheorem u : E8Packing.density = 1 := sorry", "E8Packing.density", ["E8Packing"]) == ("own", "E8Packing"),
      use("def E8Packing : Nat := 1\ntheorem u : E8Packing.density = 1 := sorry", "E8Packing.density", ["E8Packing"]))
check("binder types do not become locals", use("theorem t (s : Finset ℕ) : s.card = Finset.card s := rfl", "Finset.card") == ("mathlib", "Finset.card"),
      use("theorem t (s : Finset ℕ) : s.card = Finset.card s := rfl", "Finset.card"))
check("tactic-introduced names are local", use("theorem t : True := by\n  obtain ⟨card, hc⟩ := foo\n  exact card", "card") is None)

# ---------------------------------------------------------------- round 2 of the review
import collections, tempfile


def counts(src, own=()):
    """scan() on src as a file: (Counter of library names used, own uses, [qualified, exactly traced])."""
    X.OWN = frozenset(own)
    d = tempfile.mkdtemp()
    open(os.path.join(d, "T.lean"), "w", encoding="utf-8").write(src)
    r = X.scan((d, "T.lean"))
    cname = X.load_index()["cname"]
    return collections.Counter({cname[c]: n for c, n in r["m"]}), r["o"], r["q"]


c, _, _ = counts("open Real\ntheorem main : exp 0 = 1 := by\n  have exp : True := True.intro\n  exact Real.exp_zero")
check("a later `have` does not hide an earlier use", c["Real.exp"] == 1, c)
c, _, _ = counts("open Real\ndef t (x : Option ℝ) := match x with\n  | some exp => exp\n  | none => exp 0")
check("pattern variables stay inside their alternative", c["Real.exp"] == 1, c)
c, _, _ = counts("open Real\ntheorem main :\n    ∀ (x : Fin (Nat.floor (exp 1))), exp 0 = 1 := by\n  intro x\n  exact exp_zero")
check("names inside binder types are not locals", c["Real.exp"] == 2 and c["Nat.floor"] == 1, c)
c, _, _ = counts("theorem main : ∀ Continuous : Prop, Continuous → Continuous := by\n  intro P h\n  exact h")
check("capitalized binder names are locals", c["Continuous"] == 0, c)
c, _, _ = counts("open Real in\ndef foo :=\n  let x := exp 0\n  x\n#check exp")
check("open … in ends with its command", c["Real.exp"] == 1, c)
c, _, _ = counts("open Real in\n#check exp\ntheorem t : exp 0 = 1 := sorry")
check("open … in applies to a non-declaration command", c["Real.exp"] == 1, c)
c, _, _ = counts("open Real hiding log\nopen Real\ntheorem main : log 1 = 0 := Real.log_one")
check("hiding belongs to its own open", c["Real.log"] == 1, c)
names, aux = decl_names("structure Foo where\n  property :\n    Continuous (fun x : ℝ => x)\n\nnamespace Foo\ntheorem main : Continuous (fun x : ℝ => x) := continuous_id\nend Foo")
check("field type continuations are not fields", "Foo.Continuous" not in aux and "Foo.property" in aux, aux)
names, aux = decl_names("inductive Color where\n  | red\n  | blue\n#check match Color.red with | Color.red => 1")
check("constructors end with the inductive command", "Color.Color" not in aux, aux)
c, o, _ = counts("def wrapper := helper + 1\nwhere\n  helper := 1", ["wrapper", "wrapper.helper"])
check("where-helper used inside its owner counts as own", o == 1, (c, o))
for src, keep in [("theorem t (x : ℝ) :\n  |x| = (fun y : ℝ => |y|) x := rfl", "(fun y : ℝ => |y|) x"),
                  ("theorem t (n : Nat) : match n with\n  | 0 => True\n  | _ => True := by cases n <;> trivial", "| _ => True"),
                  ("theorem «where» : True := True.intro", "True"),
                  ("theorem t : let n := 1; n = n := rfl", "n = n")]:
    st = X.statement_of(src, "theorem")
    check("statement keeps: " + keep, keep in st and "rfl" not in st and "trivial" not in st and "True.intro" not in st, st)
names, _ = decl_names('def msg := s!"{r#"a "}"# ++ toString (Nat.succ 0)}"\ndef good := 1')
check("raw string inside an interpolation", names[-1:] == ["good"], names)
check("comment inside an interpolation", "Nat.succ" in X.strip_comments('def x := s!"{1 /- } -/ + Nat.succ 0}"'))
check("character literal inside an interpolation", "Nat.succ" in X.strip_comments("def x := s!\"{('}', Nat.succ 0)}\""))
ml = X.strip_comments('def x := s!"a\n{Nat.succ 0}"').split("\n")
check("interpolated code stays on its line", len(ml) == 2 and "Nat.succ" in ml[1], ml)
c, _, q = counts("#check Polynomial.aeval.this_lemma_was_renamed")
check("field fallback is not exact coverage", q == [1, 0], (c, q))
check("field fallback on a namespaced project constant",
      use("namespace Demo\ndef Packing : Nat := 1\n#check Packing.succ", "Packing.succ", ["Demo.Packing"]) == ("own", "Demo.Packing"),
      use("namespace Demo\ndef Packing : Nat := 1\n#check Packing.succ", "Packing.succ", ["Demo.Packing"]))
check("several modules on one import line", X.imports_of("import A B\n#check True") == ["A", "B"], X.imports_of("import A B\n#check True"))

# ---------------------------------------------------------------- round 3 of the review
c, _, _ = counts("open Real\nvariable\n  (log : ℝ → ℝ)\ntheorem main : log 1 = log 1 := rfl")
check("multi-line variable binders are locals", c["Real.log"] == 0, c)
c, _, _ = counts("open Real\ndef foo := fun\n  (exp : ℝ) => exp")
check("multi-line fun binders are locals", c["Real.exp"] == 0, c)
c, _, _ = counts("open Real in\n@[simp]\ntheorem main : exp 0 = 1 := exp_zero")
check("open … in reaches past an attribute line", c["Real.exp"] == 1 and c["Real.exp_zero"] == 1, c)
c, _, _ = counts("open Real in\nopen Nat in\n#check exp")
check("nested open … in compose", c["Real.exp"] == 1, c)
c, _, q = counts("def main (x : Option Nat) : Bool := match x with\n  | Option.some n => true\n  | Option.none => false")
check("qualified constructor patterns bind nothing themselves", c["Option.none"] == 1 and q == [2, 2], (c, q))
check("inline constructors", "Demo.mk" in decl_names("inductive Demo where | mk")[1], decl_names("inductive Demo where | mk"))
c, _, _ = counts("section\n  variable (x : ℕ)\n  #check Nat.succ\n  theorem t : Nat.succ x = x + 1 := rfl\nend")
check("an indented section after a variable line still counts", c["Nat.succ"] == 2, c)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
