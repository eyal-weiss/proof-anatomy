# Method

Proof Anatomy computes every number from the project's source text and three public files from the Mathlib project. It does not build
the project, does not run Lean, and uses no AI model. This page gives each computation exactly, then its limits.

## Inputs

- **The source.** A public `https://` link to a repository on github.com, gitlab.com or codeberg.org (cloned into a
  temporary folder and deleted afterwards) or a local folder (read in place). Anything unusual is refused; the rules are listed in the README, section "Safety".
- **The project's `.lean` files.** By default, the `lean_lib` roots and globs in `lakefile.toml` or `lakefile.lean`
  (a `.one` glob selects one file; other globs and roots select a module and its submodules), under the package's
  `srcDir`, or under the libraries' own `srcDir` when they all set the same one; every `.lean` file when the
  lakefile names none. Folders `.lake`, `lake-packages`,
  `build` and hidden folders are skipped, and symbolic links are not followed. Files that are not regular files,
  files over 20 MB and files whose scopes would need more memory than any real Lean file (more than 1,000 nested
  scopes, a namespace with more than 256 components, a namespace, declaration name or `open` command longer than
  4,096 characters, a per-file budget of namespace, `open` and declaration-name characters, or nesting deeper than
  Python's recursion limit) are skipped with a note, as is a file that takes more than 120 seconds to read (on Linux
  and macOS). A dotted name with 64 or more parts is left unresolved. The source folder must lie inside the project.
- **Mathlib's declaration table**: `declaration-data.bmp` (a JSON file) from the Mathlib documentation website, with
  the name, kind and module of every declaration in Mathlib, Lean core, Batteries and the other libraries Mathlib
  imports. Declarations under `Archive`, `Counterexamples` and `Lake` are dropped, since `import Mathlib` does not
  bring them in. The download date is recorded in `card.json` (`mathlib_table`).
- **Mathlib's lists of famous theorems**: `docs/1000.yaml` (the 1000+ theorems project) and `docs/100.yaml`
  (Freek Wiedijk's 100 theorems), which name the Mathlib declarations that prove each listed theorem.
- **When a download fails** (no network, a sandbox that allows only some sites, or a reply that is not the real file:
  a declaration table with fewer than 100,000 declarations, or a list that is not a YAML mapping), the copies in
  `proof_anatomy/data/`, downloaded on 2026-11-21, are used instead. `card.json` then records `"bundled": true`
  under `mathlib_table`, and the card's notes say so. A cached real download is kept, never replaced by the copy; runs
  that update the cache at the same time take turns; and the next run tries the download again.

## Pass 1: per-file records (`proof_anatomy/extract.py`)

For each file:

1. **Clean the text.** Remove line comments (`--`) and nested block comments (`/- -/`, including doc comments; a
   removed comment leaves a space). Empty every literal: ordinary strings, raw strings (`r"…"`, `r#"…"#`) and
   character literals such as `'"'`. Keep the code inside `{…}` of interpolated strings (`s!"…"`, `m!"…"`, `f!"…"`).
   Remove `import` lines. Every line break is kept, including those inside comments and strings.
2. **Code lines** = non-blank lines that remain.
3. **Declarations.** A line starting (after `open … in`, `set_option … in`, attributes and modifiers such as
   `private`, `noncomputable`, `protected`) with `theorem`, `lemma`, `def`, `abbrev`, `structure`, `class`,
   `inductive`, `instance`, `axiom`, `opaque` or `example` starts a declaration. Its full name joins the open
   `namespace` blocks (tracked through `namespace`, `section`, `noncomputable section`, `mutual` and `end`) with the
   written name (universe parameters `.{u}` and «quotes» removed); `_root_.` resets it. Structure fields, inductive
   constructors and `where` auxiliaries are recorded as names (kind `aux`) but not counted as declarations.
4. **Project names.** A first pass over all files collects every declaration's full name.
5. **Name resolution.** Every identifier in the code is looked up in Lean's order, using the scope in force on its
   line. Keywords, one-character names and each declaration's own name in its header are skipped, as are lines
   that only declare scope (`open`, `namespace`, `end`, `section`, `universe`, `variable`, `set_option`; for
   `open … in <command>` the command is read). The order:
   1. **Local variables visible on that line** are not looked up: the declaration's header binders and the
      `variable` binders of the enclosing sections (everywhere in the declaration); binders of `fun`, `λ`, `∀`, `∃`,
      `∑`, `∏`, `⋃`, `⋂`, `⨆`, `⨅` (the binder list may continue onto the next lines) and names introduced by common tactics (`intro`, `rintro`, `obtain`,
      `rcases … with`, `have`, `let`, `set`, `choose`, `by_cases`, `ext`, `funext`, `filter_upwards … with`), from
      the line they appear on; and pattern variables of a `| … =>` alternative, inside that alternative only. Type
      annotations inside binders are not binders. A dotted name whose head is a local (`hx.le`) is a field access on
      that local. A multi-line `variable` command is read as a whole.
   2. **The declaration itself**, for its `where` auxiliaries (`helper` inside `def wrapper … where helper := …`).
   3. **The current namespace and its parents**, innermost first (`A.B.x`, then `A.x`).
   4. **The name as written.**
   5. **Aliases** from `open X (a b)` and `open X renaming a → b`.
   6. **Opened namespaces**, in order; `open X hiding a` excludes `a` from that open only. `open scoped X` opens no
      names; `open X in` applies to the next command only (attribute lines and further `… in` prefixes before it belong to
      that command; nested ones combine), until the next line that starts a command at column 0;
      an `open` ends with the section or namespace that holds it.

   The first candidate found decides. A candidate that is a project declaration counts as an **own** use; one in
   the table counts as a **library** use, recorded with the declaration it names. When `A.b.c` is not found, `A.b`
   is tried (Lean reads `.c` as a field access), down to a two-part name, a lower-case constant, or a capitalized
   project constant (a capitalized library name such as `Real` alone is not counted, since `Real.oldName` usually
   means a lemma renamed since the project's Mathlib). Such matches count as uses, but not as exact matches for the
   coverage figure. Identifiers found
   nowhere (unrecognized locals, generated names, names renamed since the project's Mathlib) are not counted.
6. **Qualified names** (`Capitalized.head.name`: two or more parts, the head longer than one character) are counted
   separately with how many resolve exactly to the table. This is the coverage figure: the share of names a text
   scan can trace.

## Pass 2: the card (`proof_anatomy/card.py`)

**Numbers.** Files; code lines; theorems = `theorem` + `lemma` declarations; definitions = `def` + `abbrev`.

**Size against Mathlib.** The project's theorem and definition counts divided by the number of declarations of kind
`theorem` and `def` in Mathlib's table whose module is under `Mathlib/`. The two sides are counted differently
(Mathlib's from its elaborated declaration table, the project's from its source text), so the ratio is approximate.

**Areas: which parts of Mathlib the code is built on.**
For file *f* and table declaration *c* used *n(f,c)* times, the weight is
`w(f,c) = log(1 + n(f,c)) × idf(c)`, with `idf(c) = 1 + ln((1 + F) / (1 + F_c))`, where *F* is the number of project
files that use any name from the table and *F_c* the number that use *c*. Names used everywhere (such as `Nat` or
`Finset`) thus count less than names particular to a few files; the `1 +` keeps every weight positive, so a
single-file project still gets areas. Each declaration belongs to the Mathlib top-level folder of its module
(`Mathlib/Algebra/…` → Algebra; declarations outside `Mathlib/`, such as Lean core and Batteries, form one group).
A file's code lines are split among folders in proportion to its weights. A file with no name from the table takes
the mean mix of the project files that import it, processed so that importers are filled first; files reached by no
importer take the project-wide mix (files in an import cycle, which a building project cannot have, are filled
last). The card shows the share of all code lines per folder; `areas_L2` does the same for second-level folders
(`NumberTheory/LSeries`).

**Borrowed or new.** The resolved uses from pass 1, over all files, split three ways: Mathlib (modules under
`Mathlib/`), Lean core and the other libraries Mathlib uses (Batteries, Aesop, Qq and so on), and the project's own
declarations. The card shows each side's share. The detail page gives the project's share per author folder, at the
first folder level where no single folder holds more than 80% of the lines (looking at most four levels down).

**The main theorem.** The declarations in the `--main` file named by `--decl` (any kind), or else the first four
theorems and lemmas in it. The statement is the declaration's text up to the first `:=` outside brackets that does
not belong to a `let` or `have` inside the type, or up to a `| … =>` alternative (outside a `match` in the type) or a
`where` that starts the proof; for definitions, the whole text. Each identifier in it is looked up exactly as in
pass 1, with that declaration's scope and the local variables visible in the statement, and classified as Mathlib,
Lean core and other libraries, project, local, field (lower-case head with a dot) or not traced. Project definitions
are followed two levels down, up to 12 declarations per main theorem; `where` auxiliaries are not followed.
The sentence on the card is chosen by fixed rules: Mathlib definitions (plus any Lean built-ins) only; project
definitions (with the Mathlib definitions they use, up to two levels down, and whether they reach further project
definitions);
Lean built-ins only; names a text scan cannot trace (capitalized names that resolve nowhere, which a build would
explain); or notation only. The sentence names only definitions, in the order the trace meets them:
theorems used inside definitions and tactic implementations (declarations in `Mathlib/Tactic/…`, such as `positivity`
in `:= by positivity`) are shown in the trace but left out of the sentence. Axioms in the statement are named
separately ("It assumes the axiom …").

**Famous theorems.** Declarations named in `1000.yaml` or `100.yaml` (fields `decl` and `decls`) that the table holds,
of any kind (most are theorems; some entries point to a definition or an instance, such as `ZMod.chineseRemainder`
for the Chinese remainder theorem). A listed theorem is shown when at least one project file uses its declaration;
the count is the number of such files. When both lists name a declaration, the 1000+ list's title is used.

**Folder map** (detail page, for projects with at least 50 files and imports between them). The author's folders at
the first depth (1 to 3) where at least 8 folders hold 1% of the lines each (depth 3 if none does); up to 30 folders,
largest first, clockwise around a ring. A curve joins two folders when files in one import files
in the other; its width follows the number of such imports.

**History.** Only for `https://` sources: `git log` of the tool's own fresh clone (git is never run on a local
folder), with signature verification and repository-configured programs switched off (`log.showSignature=false`,
`core.fsmonitor=false`, no hooks) and every network transport disabled (`GIT_ALLOW_PROTOCOL` empty,
`protocol.allow=never`, no lazy fetching), so a repository's own git configuration cannot launch anything, for example
through a promisor remote with its own ssh command. The output is read as bytes, split on line feeds only, and a row
counts only when its date is a real calendar date. Dates, author names and e-mail addresses are read locally; only
counts are written. Commits by bot accounts are dropped: `[bot]` in the name or address, an address whose local part
is `bot` or ends in `bot` after `-`, `_`, `.` or `+`, and named accounts such as `github-actions`, `dependabot` and
`mathlib-bors`. Shown with at least 20 commits over at least 3 months. Contributors = distinct author e-mail
addresses.

**Pins.** The Mathlib commit in `lake-manifest.json` and the Lean version in `lean-toolchain`, each when present,
read only from ordinary files inside the project and kept only when they look like a commit or a version name.

## Pass 3: comparison (`proof_anatomy/corpus.py`)

Each card's `areas_L2` is normalized to sum to 1. Similarity = cosine similarity of these vectors (0 = no folder in
common, 1 = the same mix). The comparison page orders projects by average-linkage hierarchical clustering on
1 − similarity, and lists families by cutting the tree at the largest gap between successive merge heights (with at
least three projects). The Lean core group is one of the dimensions. Cards with no Mathlib area at all (only Lean
core and other libraries) are left out of the comparison. Similarities depend on the
projects in the set; the card says how many were compared.

## Tests

`tests/test_extract.py` checks the scanner and the name lookup on small Lean snippets: literals that must not swallow
code, declaration names, statement extraction, and each step of the lookup order. `tests/test_wrapper.py` checks
lakefile reading and argument checks. `tests/test_security.py` replays each attack found in the pre-release security
review: a repository's git config running a command while history is read, forged history rows, linked metadata
files, lakefile entries posing as options, outputs written through links, credentials in URLs, thousands of nested
namespaces or interpolated strings, and a FIFO named `.lean`.

Cards do not depend on the number of processes: per-file records are written in file order and ties are broken by
name.

## Limits

- **Text, not elaboration.** Names generated by Lean (instances found by type class search, auto-bound implicits,
  names produced by macros or attributes), names used only through tactics or notation, and dot-notation calls on
  variables (`h.trans`) cannot be resolved without a build. They are absent from library uses, own uses and areas.
- **Approximate name lookup.** The lookup does not model `export`, an `open` of a namespace written relative to the
  current one, private names, or overloading: where Lean would report an ambiguity between the name as written and
  an opened namespace, the name as written wins. Local variables are recognized from common binder forms only, with
  line-level scope (a `have` inside one branch of a proof stays visible in later branches); a local introduced by an
  unusual tactic and named like a global counts as a use of that global.
- **Today's Mathlib.** Names are looked up in the current table while each project pins an older Mathlib; names
  renamed or deprecated since then do not resolve. The coverage figure shows the effect.
- **Line-based parsing.** A declaration is recognized when its keyword starts a line (after the prefixes above);
  declarations generated by macros or custom commands are not counted, and a command quoted inside metaprogramming
  code can confuse the namespace tracking.
- **Areas measure foundations.** They show which parts of Mathlib a project's code calls, not the research field
  of its result or where the difficulty lies.
- **Resemblance is relative.** It compares mixes of Mathlib folders, so two projects built on similar foundations
  look alike even when they prove unrelated theorems.
