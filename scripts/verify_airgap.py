#!/usr/bin/env python3
"""Seat 5 · deployment — the air-gap proof.

ISRO evaluation machines are not on the internet. "Air-gapped" is therefore a claim
this repo makes, and a claim nobody checks is a claim that is already false. This
script checks the two ways the claim actually breaks:

  1. A generated HTML artifact pulls a script, a stylesheet or a font from a CDN.
     It renders beautifully on the laptop that built it and renders as unstyled
     text on the machine that matters.
  2. A module imports a package that is not in requirements.lock. It works in the
     dev venv and ImportErrors inside the container.

Exit code is the product: 0 clean, 1 dirty. CI gates on it.

    python scripts/verify_airgap.py [--root .] [-v]
"""

import argparse
import ast
import os
import re
import sys
from importlib import metadata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories that are not ours to police: the venv is vendored third-party code,
# .git is opaque, caches are derived.
SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".cache",
             ".mypy_cache", ".ruff_cache", "node_modules", "site-packages"}

# An absolute URL, or a protocol-relative one (//cdn.example.com/x.js), which is the
# form people reach for when they "remove the https" and think that made it local.
_ABS_URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)
_PROTOCOL_RELATIVE = re.compile(r"""(?:src|href)\s*=\s*["']//[^"']+""", re.I)

# xmlns="http://www.w3.org/2000/svg" is a namespace *identifier*, not a fetch. Nothing
# is ever requested from it. Matplotlib's SVG output carries one, so exempting it is
# the difference between a useful gate and one people learn to ignore.
_NAMESPACE_DECL = re.compile(r"""xmlns(?::\w+)?\s*=\s*["']https?://[^"']*["']""", re.I)

# An embedded data: URI is, by definition, the thing we are asking for — the asset is in
# the file. A report.html is ~4 MB of base64 PNG and ~50 KB of actual markup, so dropping
# the payload before scanning is both faster (7.7 s -> 0.2 s over the run directory) and
# more correct: a chance "http" inside base64 is not an external reference.
_DATA_URI = re.compile(r"""data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{64,}""", re.I)


# Web assets a browser would fetch. Today everything is inlined into .html, but the gate
# must not go blind the moment somebody splits out a viewer/app.js — which is exactly the
# commit that would reintroduce a CDN <script>.
WEB_SUFFIXES = (".html", ".htm", ".css", ".js", ".svg")


def _walk(root, suffixes):
    if isinstance(suffixes, str):
        suffixes = (suffixes,)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(suffixes):
                yield os.path.join(dirpath, name)


# ---------------------------------------------------------------- HTML artifacts

def scan_html(root):
    """Every web asset under root. Returns (files_checked, [(path, lineno, snippet)])."""
    checked, offences = [], []
    for path in _walk(root, WEB_SUFFIXES):
        checked.append(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:                     # unreadable is not "clean"
            offences.append((path, 0, f"unreadable: {exc}"))
            continue
        text = _DATA_URI.sub("data:[inlined]", text)
        if "http" not in text and "//" not in text:
            continue                               # nothing in here can be a URL
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = _NAMESPACE_DECL.sub("", line)
            for match in _ABS_URL.findall(stripped) + _PROTOCOL_RELATIVE.findall(stripped):
                offences.append((path, lineno, match.strip()[:110]))
    return checked, offences


# ---------------------------------------------------------------- imports

def locked_distributions(lock_path):
    """Canonical distribution names in requirements.lock, plus the project itself."""
    names = {"samanvay"}
    with open(lock_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if line:
                names.add(_canon(line.split("==")[0]))
    return names


def _canon(name):
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _local_top_levels(root):
    """Top-level names importable from the repo root — our own packages and modules."""
    local = set()
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if entry in SKIP_DIRS or entry.startswith("."):
            continue
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            local.add(entry)
        elif os.path.isdir(full) and any(n.endswith(".py") for n in os.listdir(full)):
            local.add(entry)                       # namespace-ish dirs: bench/, synth/, tests/
        elif entry.endswith(".py"):
            local.add(entry[:-3])
    return local


def _top_level_imports(path):
    """Top-level module name of every import in one file. Syntax errors are reported."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:    # level > 0 is relative: always local
                found.add(node.module.split(".")[0])
    return found, None


def _sibling_modules(directory):
    """Names importable as bare modules from a file's own directory. pytest inserts each
    test directory on sys.path, so `import test_photometry` from tests/test_iterate.py is
    an ordinary local import — not a missing dependency."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return set()
    names = {e[:-3] for e in entries if e.endswith(".py")}
    names |= {e for e in entries
              if os.path.isfile(os.path.join(directory, e, "__init__.py"))}
    return names


def scan_imports(root, locked):
    """Returns (files_checked, [(path, module, reason)])."""
    stdlib = set(sys.stdlib_module_names)
    local = _local_top_levels(root)
    siblings = {}                                  # directory -> its bare-importable names
    # import name -> distributions that provide it (cv2 -> opencv-python-headless,
    # skimage -> scikit-image, yaml -> PyYAML). Reading the map beats hardcoding it.
    provided = metadata.packages_distributions()

    checked, offences = [], []
    for path in _walk(root, ".py"):
        checked.append(path)
        modules, error = _top_level_imports(path)
        if error:
            offences.append((path, "-", error))
            continue
        here = os.path.dirname(path)
        if here not in siblings:
            siblings[here] = _sibling_modules(here)
        for module in sorted(modules):
            if (module in stdlib or module in local or module.startswith("_")
                    or module in siblings[here]):
                continue
            dists = [_canon(d) for d in provided.get(module, [])]
            if not dists:
                offences.append((path, module,
                                 "not installed here, so not covered by requirements.lock"))
            elif not any(d in locked for d in dists):
                offences.append((path, module,
                                 f"provided by {dists}, none of which is in requirements.lock"))
    return checked, offences


# ---------------------------------------------------------------- report

def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify SAMANVAY is air-gap clean.")
    ap.add_argument("--root", default=REPO, help="Repository root (default: this repo)")
    ap.add_argument("-v", "--verbose", action="store_true", help="List every file checked")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)

    lock_path = os.path.join(root, "requirements.lock")
    if not os.path.isfile(lock_path):
        print(f"FAIL  requirements.lock missing at {lock_path}")
        return 1
    locked = locked_distributions(lock_path)

    html_files, html_bad = scan_html(root)
    py_files, import_bad = scan_imports(root, locked)

    if args.verbose:
        for path in html_files + py_files:
            print(f"  checked {os.path.relpath(path, root)}")

    print(f"air-gap check · {os.path.relpath(root, os.getcwd()) or '.'}")
    print(f"  web assets scanned      : {len(html_files)}")
    print(f"  Python modules scanned  : {len(py_files)}")
    print(f"  packages in lock        : {len(locked) - 1}")

    if html_bad:
        print(f"\nFAIL  {len(html_bad)} external reference(s) in generated web assets:")
        for path, lineno, snippet in html_bad:
            print(f"    {os.path.relpath(path, root)}:{lineno}  {snippet}")
        print("    Inline the asset (or embed it as a data: URI). An air-gapped judge")
        print("    sees an unstyled page and a blank viewer, with no error to explain it.")

    if import_bad:
        print(f"\nFAIL  {len(import_bad)} unlocked import(s):")
        for path, module, reason in import_bad:
            print(f"    {os.path.relpath(path, root)}  imports {module!r} — {reason}")
        print("    Either drop the import or regenerate requirements.lock from an env")
        print("    that has it; the container installs the lock and nothing else.")

    if html_bad or import_bad:
        print(f"\nAIR-GAP: FAIL  ({len(html_bad)} external URL, {len(import_bad)} unlocked import)")
        return 1

    print("\nAIR-GAP: PASS  (no external URL, no unlocked import)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
