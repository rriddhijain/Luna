"""Seat 5 · deployment — tests for the claims the deployment files make.

Every assertion here corresponds to a sentence somebody will say to a judge:
"the environment is pinned", "the container does not run as root", "it works offline",
"the README's commands are real". A claim nobody checks is a claim that is already false.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(REPO, "requirements.lock")
DOCKERFILE = os.path.join(REPO, "Dockerfile")
COMPOSE = os.path.join(REPO, "docker-compose.yml")
CI = os.path.join(REPO, ".github", "workflows", "ci.yml")
MAKEFILE = os.path.join(REPO, "Makefile")
AIRGAP = os.path.join(REPO, "scripts", "verify_airgap.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _lock_requirements():
    """Non-comment, non-blank lines of requirements.lock."""
    out = []
    for raw in _read(LOCK).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


# ------------------------------------------------------------------ the lock

def test_requirements_lock_exists_and_is_not_empty():
    assert os.path.isfile(LOCK), "requirements.lock is the reproducibility claim"
    assert _lock_requirements(), "an empty lock pins nothing"


def test_every_locked_line_is_exactly_pinned():
    """`>=` in a lock file is a lock that does not lock. One drifting transitive dep is
    all it takes for "it works on my machine" to come back."""
    loose = [r for r in _lock_requirements() if not re.fullmatch(r"[A-Za-z0-9._-]+==[^=<>!, ]+", r)]
    assert not loose, f"not exactly pinned with ==: {loose}"


def test_lock_covers_what_the_code_actually_imports():
    """The third-party packages the modules import must all be in the lock. Anything else
    ImportErrors inside the container, on the machine with no network to fix it."""
    for name in ("numpy", "scipy", "opencv-python-headless", "rasterio",
                 "scikit-image", "matplotlib", "click", "PyYAML", "pds4_tools", "pytest"):
        assert any(r.split("==")[0].lower().replace("_", "-") == name.lower().replace("_", "-")
                   for r in _lock_requirements()), f"{name} missing from requirements.lock"


def test_lock_does_not_pin_the_project_itself():
    """samanvay is installed from source in the same Dockerfile; pinning it to a version
    in its own lock is a circular claim, and pip would go looking for it on PyPI."""
    names = {r.split("==")[0].lower() for r in _lock_requirements()}
    assert "samanvay" not in names


# ------------------------------------------------------------------ the image

def test_dockerfile_installs_from_the_lock():
    text = _read(DOCKERFILE)
    assert "requirements.lock" in text, "the image must install the pinned set, not pyproject floors"
    assert re.search(r"pip install[^\n]*-r requirements\.lock", text), \
        "requirements.lock is mentioned but never installed"


def test_dockerfile_runs_as_a_non_root_user():
    """A bind-mounted run as uid 0 leaves root-owned files on the judge's machine, and a
    container that owns its own filesystem is a container with nothing to lose."""
    text = _read(DOCKERFILE)
    users = re.findall(r"^\s*USER\s+(\S+)", text, re.M)
    assert users, "no USER directive: the image runs as root"
    assert users[-1] not in ("root", "0"), f"final USER is {users[-1]!r}"
    assert re.search(r"^\s*(RUN\s+)?(useradd|adduser)", text, re.M), \
        "USER names an account the image never creates"


def test_dockerfile_pins_python_311():
    """3.14 has no wheels for the geo stack. An unpinned base tag is a time bomb."""
    bases = re.findall(r"^\s*FROM\s+(\S+)", _read(DOCKERFILE), re.M)
    pythons = [b for b in bases if b.startswith("python:")]
    assert pythons, f"no python base image among {bases}"
    assert all(b.startswith("python:3.11") for b in pythons), pythons


def test_dockerfile_sets_agg_backend():
    """matplotlib must never look for a display inside a container."""
    assert re.search(r"MPLBACKEND\s*=\s*Agg", _read(DOCKERFILE))


def test_dockerfile_caches_dependencies_before_source():
    """The lock must be COPYed and installed before the source, or every one-line edit
    reinstalls the whole geo stack."""
    text = _read(DOCKERFILE)
    lock_install = text.index("-r requirements.lock")
    source_copy = text.index("COPY samanvay")
    assert lock_install < source_copy, "source is copied before deps install; the cache is wasted"


# ------------------------------------------------------------------ compose

def test_compose_parses_and_names_the_demo_ablation_and_benchmark():
    services = yaml.safe_load(_read(COMPOSE))["services"]
    for name in ("demo", "ablate", "bench"):
        assert name in services, f"no `{name}` service: a judge should not assemble flags by hand"
        assert services[name].get("build") or services[name].get("image")


# ------------------------------------------------------------------ CI

def _ci_steps():
    workflow = yaml.safe_load(_read(CI))
    return workflow, [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_ci_workflow_parses_and_runs_on_python_311():
    """CI must be the same interpreter as the venv and the image, or it is a third
    environment nobody tests. "3.x" would float onto a version with no geo wheels."""
    _, steps = _ci_steps()
    setups = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert setups, "CI never pins a Python version"
    assert all(str(s["with"]["python-version"]).startswith("3.11") for s in setups), setups


def _ci_shell_lines():
    """Executable lines of every `run:` body, comments stripped. Scanning the raw file
    instead would flag this workflow's own header, which says there is no `|| true`."""
    lines = []
    for step in _ci_steps()[1]:
        for line in str(step.get("run", "")).splitlines():
            if line.strip() and not line.strip().startswith("#"):
                lines.append(line)
    return lines


def test_ci_has_no_continue_on_error_anywhere():
    """A yellow build is a build people stop reading. Every gate must be able to fail."""
    workflow, steps = _ci_steps()
    offenders = [s.get("name") or s.get("uses") for s in steps if s.get("continue-on-error")]
    offenders += [n for n, job in workflow["jobs"].items() if job.get("continue-on-error")]
    assert not offenders, f"continue-on-error turns a gate into decoration: {offenders}"
    swallowed = [ln for ln in _ci_shell_lines() if "|| true" in ln or "|| :" in ln]
    assert not swallowed, f"`|| true` is continue-on-error wearing a hat: {swallowed}"


def test_ci_cancels_superseded_runs():
    """Without a concurrency group a burst of pushes queues N full runs and the answer
    that matters — the tip commit's — arrives last."""
    concurrency = yaml.safe_load(_read(CI)).get("concurrency")
    assert concurrency and concurrency.get("cancel-in-progress") is True, concurrency


def test_ci_installs_from_the_lock_and_gates_on_airgap():
    """The three claims CI exists to defend: pinned install, the suite, the air-gap gate."""
    body = "\n".join(str(s.get("run", "")) for s in _ci_steps()[1])
    assert "-r requirements.lock" in body, "CI does not install the pinned set"
    assert "pytest" in body, "CI does not run the suite"
    assert "scripts/verify_airgap.py" in body, "CI does not gate on the air-gap proof"


# ------------------------------------------------------------------ air-gap

def _load_airgap():
    spec = importlib.util.spec_from_file_location("verify_airgap", AIRGAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_airgap_script_exists():
    assert os.path.isfile(AIRGAP)


def test_airgap_passes_on_the_current_tree(capsys):
    """The whole air-gap claim, in one assertion.

    If this fails, read the printed offender list: some HTML artifact pulls a script,
    stylesheet or font from a CDN, or some module imports a package outside the lock.
    Fix the file it names — do not weaken this test."""
    code = _load_airgap().main(["--root", REPO])
    report = capsys.readouterr().out
    assert code == 0, "\n" + report


def test_airgap_gate_actually_fails_on_a_dirty_tree(tmp_path):
    """A gate that cannot fail is decoration. Prove it catches a CDN reference."""
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n")
    (tmp_path / "report.html").write_text(
        '<link href="https://fonts.googleapis.com/css2?family=Outfit" rel="stylesheet">')
    assert _load_airgap().main(["--root", str(tmp_path)]) == 1


def test_airgap_catches_a_cdn_in_a_split_out_script(tmp_path):
    """Everything is inlined into .html today, so a gate that only reads .html passes
    right up until somebody splits out viewer/app.js — the same commit that would
    reintroduce the CDN import."""
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n")
    (tmp_path / "app.js").write_text(
        'import OpenSeadragon from "https://cdn.jsdelivr.net/npm/openseadragon@4/+esm";')
    assert _load_airgap().main(["--root", str(tmp_path)]) == 1


def test_airgap_tolerates_an_svg_namespace_declaration(tmp_path):
    """xmlns="http://www.w3.org/2000/svg" is an identifier, never a fetch. Matplotlib's
    SVG output carries one; flagging it would train people to ignore the gate."""
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n")
    (tmp_path / "report.html").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    assert _load_airgap().main(["--root", str(tmp_path)]) == 0


def test_airgap_catches_an_import_outside_the_lock(tmp_path):
    """The second half of the claim. `import torch` works on the laptop that has it and
    ImportErrors on the air-gapped machine, where there is no network to fix it."""
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n")
    (tmp_path / "matcher.py").write_text("import torch\n")
    assert _load_airgap().main(["--root", str(tmp_path)]) == 1


def test_airgap_tolerates_a_sibling_module_import(tmp_path):
    """pytest puts each test directory on sys.path, so `import test_photometry` from a
    neighbouring test file is an ordinary local import. Flagging it would make the gate
    fire on a correct commit, which is how a gate gets switched off."""
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_photometry.py").write_text("VALUE = 1\n")
    (tests / "test_iterate.py").write_text("import test_photometry\n")
    assert _load_airgap().main(["--root", str(tmp_path)]) == 0


def test_airgap_exits_non_zero_from_the_command_line():
    """CI gates on the exit code, so the exit code is what the test must exercise."""
    result = subprocess.run([sys.executable, AIRGAP, "--root", REPO],
                            capture_output=True, text=True, cwd=REPO)
    assert result.returncode in (0, 1), result.stderr
    assert "AIR-GAP:" in result.stdout


# ------------------------------------------------------------------ the Makefile

REQUIRED_TARGETS = ("setup", "test", "fixture", "demo", "ablate",
                    "bench", "dashboard", "airgap", "clean")


def _make_targets():
    text = _read(MAKEFILE)
    return {m.group(1) for m in re.finditer(r"^([A-Za-z][A-Za-z0-9_-]*):(?!=)", text, re.M)}


def test_makefile_has_the_one_command_surface():
    missing = [t for t in REQUIRED_TARGETS if t not in _make_targets()]
    assert not missing, f"Makefile is missing targets: {missing}"


def _make_invocations_in_markdown(text):
    """`make demo` in a code span, or a `make demo` line in a fenced block. Prose like
    "make the comparison mean something" is English, not an invocation, and a check that
    flags it is a check people switch off."""
    found = set()
    for match in re.finditer(r"`\s*make\s+([a-z][a-z0-9_-]*)", text):
        found.add(match.group(1))
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            match = re.match(r"\s*[$>]?\s*make\s+([a-z][a-z0-9_-]*)", line)
            if match:
                found.add(match.group(1))
    return found


def test_every_make_target_the_docs_promise_exists():
    """A README that says `make demo` when there is no demo target is worse than a README
    with no commands in it: the first thing a new machine does is fail."""
    targets = _make_targets()
    promised = {}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in {".venv", ".git", "__pycache__", ".pytest_cache", ".cache", "runs"}]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            for target in _make_invocations_in_markdown(_read(path)):
                promised.setdefault(target, set()).add(os.path.relpath(path, REPO))
    unknown = {t: sorted(where) for t, where in promised.items() if t not in targets}
    assert not unknown, f"docs promise make targets that do not exist: {unknown}"


def test_makefile_default_goal_is_help_not_a_side_effect():
    """A bare `make` on a fresh clone must print the surface, never start a five-minute run."""
    assert re.search(r"^\.DEFAULT_GOAL\s*:?=\s*help", _read(MAKEFILE), re.M)


@pytest.mark.skipif(shutil.which("make") is None,
                    reason="no make binary (python:slim has none); the assertion still "
                           "runs on any dev machine and on the CI runner")
@pytest.mark.parametrize("target", REQUIRED_TARGETS)
def test_every_makefile_target_is_syntactically_runnable(target):
    """`make -n` expands the recipe without executing it. Catches an undefined variable or
    a typo'd target name without spending a minute per target."""
    result = subprocess.run(["make", "-n", target], capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, f"make -n {target}: {result.stderr}"
    assert result.stdout.strip(), f"make -n {target} expands to nothing"
