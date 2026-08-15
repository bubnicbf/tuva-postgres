#!/usr/bin/env python3
"""Structural regression test for the repository's Python dependency setup.

The application scripts under scripts/ use only the Python standard
library; Ruff, mypy, pytest, SQLFluff, pre-commit, PyYAML, and
types-requests are dev/tooling dependencies. This test guards the
reproducible-toolchain contract described in the repository README: exact
direct pins in pyproject.toml for the full toolchain (not only SQLFluff
and pre-commit), a current committed uv.lock that actually contains those
exact versions, a single selected Python version (.python-version)
compatible with pyproject.toml's requires-python, and no ad hoc/unpinned
`pip install ruff`/`mypy`/`pytest`/`sqlfluff`/`pre-commit` left behind in
the Makefile or CI.

Standard library only, including `tomllib` (Python 3.11+) -- no PyYAML,
uv, pre-commit, sqlfluff, or network access required. Because this test
must remain runnable *before* `uv sync` has ever been run (see
Makefile's test-shell target), it deliberately only reads files; it never
imports or invokes uv/pre-commit/sqlfluff itself.

This is not a full PEP 440 version-specifier implementation -- it parses
just the exact-pin form this repository actually uses
(`name==X.Y.Z`) plus a minimal numeric comparator for the small set of
requires-python operators (==, !=, <=, >=, <, >) needed to check that the
selected .python-version satisfies requires-python.

Usage:
    python3 scripts/tests/test_python_dependencies.py [repo_root]

With no argument, validates the real repository (located relative to this
script's own path). An optional repo_root argument lets a negative
control point this test at a scratch fixture directory instead, without
ever touching the real, committed files.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REQUIRED_DEV_PACKAGES = [
    "ruff",
    "mypy",
    "pytest",
    "types-requests",
    "sqlfluff",
    "pre-commit",
    "pyyaml",
]

# Packages checked for both an exact pin in pyproject.toml AND a matching
# entry in uv.lock (every REQUIRED_DEV_PACKAGES entry qualifies -- kept as
# its own name so the intent of the lock cross-check is explicit at the
# call site below).
LOCK_CHECKED_DEV_PACKAGES = REQUIRED_DEV_PACKAGES

# Tool names that must never appear in an ad hoc, unpinned `pip install`
# anywhere in the Makefile or CI workflow -- they must come from
# `uv sync --locked` only.
GUARDED_TOOL_NAMES = ["ruff", "mypy", "pytest", "sqlfluff", "pre-commit", "pre_commit"]

# Tool names that must be invoked through `uv run` in CI (never a bare
# `ruff`/`mypy`/`pytest`/`sqlfluff` relying on some other, unlocked
# installation on the runner's PATH).
UV_RUN_CHECKED_TOOL_NAMES = ["ruff", "mypy", "pytest", "sqlfluff"]

EXACT_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^=<>!~,\s\[\]]+)$")
REQUIRES_PYTHON_CLAUSE_RE = re.compile(r"^(==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*)\*?$")
PIP_INSTALL_RE = re.compile(r"pip\s+install", re.IGNORECASE)


MAKE_TARGET_HEADER_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)\s*(.*)$")
MAKE_INVOCATION_RE = re.compile(r"\bmake\s+((?:[A-Za-z0-9_.-]+\s*)+)")


class DependencyError(Exception):
    """Raised for a structural problem in the dependency setup, with a
    message that names the specific file/package/version at fault."""


def parse_makefile_targets(text: str):
    """Return {target_name: {"prereqs": [...], "recipe": [line, ...]}} for a
    Makefile's explicit targets. Deliberately simple: single-name target
    lines with tab-indented recipe bodies (this repository's actual style,
    confirmed by inspection) -- not a general Makefile parser. Skips
    special targets (e.g. .PHONY) and variable assignments."""
    targets = {}
    current = None
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            if current is not None:
                targets[current]["recipe"].append(raw_line[1:])
            continue
        if not raw_line.strip():
            current = None
            continue
        line = raw_line.split("#", 1)[0].rstrip()
        m = MAKE_TARGET_HEADER_RE.match(line)
        if m:
            name, prereqs_text = m.groups()
            if name.startswith("."):
                current = None
                continue
            entry = targets.setdefault(name, {"prereqs": [], "recipe": []})
            entry["prereqs"].extend(prereqs_text.split())
            current = name
        else:
            current = None
    return targets


def target_recipe_contains(targets, target_name, predicate, _visited=None):
    """True iff any recipe line of target_name, or of any prerequisite
    target (recursively, e.g. `quality`'s dependency chain), satisfies
    predicate(line). Guards against prerequisite cycles."""
    if _visited is None:
        _visited = set()
    if target_name in _visited or target_name not in targets:
        return False
    _visited.add(target_name)
    info = targets[target_name]
    if any(predicate(line) for line in info["recipe"]):
        return True
    return any(
        target_recipe_contains(targets, prereq, predicate, _visited)
        for prereq in info["prereqs"]
    )


def _version_tuple(text: str) -> tuple:
    return tuple(int(p) for p in text.split("."))


def _pad(a: tuple, b: tuple) -> tuple:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def _compare(op: str, actual: tuple, required: tuple) -> bool:
    a, r = _pad(actual, required)
    if op == "==":
        return a == r
    if op == "!=":
        return a != r
    if op == "<=":
        return a <= r
    if op == ">=":
        return a >= r
    if op == "<":
        return a < r
    if op == ">":
        return a > r
    raise DependencyError(f"unsupported requires-python operator: {op!r}")


def python_version_satisfies(selected: str, requires_python: str) -> bool:
    """True iff the selected X.Y version satisfies every comma-separated
    clause of a requires-python specifier built from ==,!=,<=,>=,<,> only.
    """
    selected_tuple = _version_tuple(selected)
    for raw_clause in requires_python.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        m = REQUIRES_PYTHON_CLAUSE_RE.match(clause)
        if not m:
            raise DependencyError(
                f"pyproject.toml requires-python clause {clause!r} is not one of "
                "==,!=,<=,>=,<,> with a dotted numeric version (this test does not "
                "implement the full PEP 440 specifier grammar)"
            )
        op, version_text = m.groups()
        if not _compare(op, selected_tuple, _version_tuple(version_text)):
            return False
    return True


def parse_exact_pins(entries, group_label: str):
    """Return {package_name: version} for a list of 'name==version' strings.

    Raises DependencyError (naming the offending entry) for anything that
    isn't an exact, single-version pin -- e.g. `sqlfluff`, `sqlfluff>=1`,
    `sqlfluff==1,!=1.1`, or an extras marker.
    """
    pins = {}
    for entry in entries:
        m = EXACT_PIN_RE.match(entry.strip())
        if not m:
            raise DependencyError(
                f"{group_label} entry {entry!r} is not an exact pin of the form "
                "'name==version'"
            )
        name, version = m.groups()
        pins[name] = version
    return pins


def find_lockfile_versions(lock_data: dict, package_name: str):
    """Return the list of versions found for package_name in a parsed
    uv.lock (TOML) document's [[package]] entries."""
    versions = []
    for pkg in lock_data.get("package", []):
        if pkg.get("name") == package_name:
            v = pkg.get("version")
            if v:
                versions.append(v)
    return versions


def validate(repo_root: Path):
    diagnostics = []

    pyproject_path = repo_root / "pyproject.toml"
    lock_path = repo_root / "uv.lock"
    python_version_path = repo_root / ".python-version"
    makefile_path = repo_root / "Makefile"
    ci_path = repo_root / ".github" / "workflows" / "ci.yml"

    # --- pyproject.toml: [project] table -----------------------------------
    if not pyproject_path.is_file():
        raise DependencyError(f"pyproject.toml not found: {pyproject_path}")
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)

    if "project" not in pyproject:
        raise DependencyError("pyproject.toml has no [project] table")
    project = pyproject["project"]

    if "requires-python" not in project or not str(project["requires-python"]).strip():
        raise DependencyError("pyproject.toml [project] is missing 'requires-python'")
    requires_python = str(project["requires-python"]).strip()

    if "dependencies" not in project:
        raise DependencyError(
            "pyproject.toml [project] is missing 'dependencies' "
            "(must be present, even as an empty list, for a project with no "
            "third-party runtime dependencies)"
        )
    if not isinstance(project["dependencies"], list):
        raise DependencyError("pyproject.toml [project].dependencies must be a list")

    # --- .python-version selected, compatible with requires-python ---------
    if not python_version_path.is_file():
        raise DependencyError(f".python-version not found: {python_version_path}")
    selected_python = python_version_path.read_text(encoding="utf-8").strip()
    if not re.match(r"^[0-9]+(\.[0-9]+)*$", selected_python):
        raise DependencyError(
            f".python-version contains {selected_python!r}, expected a bare "
            "dotted version like '3.12'"
        )
    if not python_version_satisfies(selected_python, requires_python):
        raise DependencyError(
            f".python-version selects {selected_python!r}, which does not satisfy "
            f"pyproject.toml's requires-python = {requires_python!r}"
        )

    # --- [dependency-groups] dev: sqlfluff + pre-commit, exact pins --------
    dependency_groups = pyproject.get("dependency-groups")
    if not dependency_groups or "dev" not in dependency_groups:
        raise DependencyError(
            "pyproject.toml is missing a [dependency-groups] 'dev' group"
        )
    dev_entries = dependency_groups["dev"]
    if not isinstance(dev_entries, list) or not dev_entries:
        raise DependencyError("pyproject.toml [dependency-groups].dev must be a non-empty list")

    pins = parse_exact_pins(dev_entries, "pyproject.toml [dependency-groups].dev")

    counts = {}
    for entry in dev_entries:
        m = EXACT_PIN_RE.match(entry.strip())
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1

    for pkg in REQUIRED_DEV_PACKAGES:
        if pkg not in pins:
            raise DependencyError(
                f"pyproject.toml [dependency-groups].dev is missing an exact pin for {pkg!r}"
            )
        if counts.get(pkg, 0) > 1:
            raise DependencyError(
                f"pyproject.toml [dependency-groups].dev declares {pkg!r} "
                f"{counts[pkg]} times (expected exactly once)"
            )

    sqlfluff_version = pins["sqlfluff"]
    pre_commit_version = pins["pre-commit"]
    ruff_version = pins["ruff"]
    mypy_version = pins["mypy"]
    pytest_version = pins["pytest"]
    types_requests_version = pins["types-requests"]
    pyyaml_version = pins["pyyaml"]

    # --- uv.lock exists and contains the selected versions ------------------
    if not lock_path.is_file():
        raise DependencyError(f"uv.lock not found: {lock_path} (run 'uv lock' and commit it)")
    with lock_path.open("rb") as f:
        lock_data = tomllib.load(f)

    for pkg in LOCK_CHECKED_DEV_PACKAGES:
        pinned_version = pins[pkg]
        lock_versions = find_lockfile_versions(lock_data, pkg)
        if not lock_versions:
            raise DependencyError(f"uv.lock has no [[package]] entry named {pkg!r}")
        if pinned_version not in lock_versions:
            raise DependencyError(
                f"uv.lock has {pkg}=={'/'.join(lock_versions)}, but pyproject.toml "
                f"pins {pkg}=={pinned_version} (run 'uv lock' to bring them back in sync)"
            )

    # --- Makefile: no ad hoc unpinned tool installation ---------------------
    if not makefile_path.is_file():
        raise DependencyError(f"Makefile not found: {makefile_path}")
    makefile_text = makefile_path.read_text(encoding="utf-8")
    for line_no, line in enumerate(makefile_text.splitlines(), start=1):
        if PIP_INSTALL_RE.search(line) and any(
            name in line.lower() for name in GUARDED_TOOL_NAMES
        ):
            raise DependencyError(
                f"Makefile:{line_no} contains an ad hoc unpinned installation: {line.strip()!r} "
                "(Ruff/mypy/pytest/SQLFluff/pre-commit must come from 'uv sync --locked' only)"
            )

    # --- CI: no unpinned pip install of a guarded tool, uses locked uv sync,
    #     and runs each guarded tool through uv run -----------------------
    if not ci_path.is_file():
        raise DependencyError(f"CI workflow not found: {ci_path}")
    ci_text = ci_path.read_text(encoding="utf-8")
    for line_no, line in enumerate(ci_text.splitlines(), start=1):
        if PIP_INSTALL_RE.search(line) and any(
            name in line.lower() for name in GUARDED_TOOL_NAMES
        ):
            raise DependencyError(
                f".github/workflows/ci.yml:{line_no} still contains an unpinned "
                f"pip install of a locked tool: {line.strip()!r}"
            )

    if "uv sync --locked" not in ci_text:
        raise DependencyError(
            ".github/workflows/ci.yml does not run a locked 'uv sync --locked'"
        )

    # A CI step may invoke a tool directly (`uv run ruff ...`) or indirectly
    # through a `make` target whose recipe (or one of its prerequisite
    # targets' recipes, e.g. `quality`'s dependency chain) itself runs the
    # tool via `uv run`. Both count -- what matters is that the tool is
    # never invoked any other way (an unpinned PATH lookup, a bare pip
    # install, etc.), which the checks above already rule out.
    make_targets = parse_makefile_targets(makefile_text)
    ci_lines = ci_text.splitlines()
    invoked_make_targets = set()
    for line in ci_lines:
        for m in MAKE_INVOCATION_RE.finditer(line):
            invoked_make_targets.update(m.group(1).split())

    for tool_name in UV_RUN_CHECKED_TOOL_NAMES:

        def _line_runs_tool_via_uv_run(line, tool_name=tool_name):
            return "uv run" in line and tool_name in line.lower()

        direct = any(_line_runs_tool_via_uv_run(line) for line in ci_lines)
        via_make = any(
            target_recipe_contains(make_targets, target, _line_runs_tool_via_uv_run)
            for target in invoked_make_targets
        )
        if not (direct or via_make):
            raise DependencyError(
                f".github/workflows/ci.yml does not run {tool_name} through 'uv run' "
                "(directly, or indirectly via a 'make' target -- including its "
                "prerequisite targets -- whose recipe uses 'uv run')"
            )

    return [
        f"pyproject.toml: [project] present, requires-python = {requires_python!r}, "
        "dependencies explicitly declared.",
        f".python-version: {selected_python!r} satisfies requires-python.",
        "[dependency-groups].dev: exact pins, each declared once, for "
        f"ruff=={ruff_version}, mypy=={mypy_version}, pytest=={pytest_version}, "
        f"types-requests=={types_requests_version}, sqlfluff=={sqlfluff_version}, "
        f"pre-commit=={pre_commit_version}, pyyaml=={pyyaml_version}.",
        "uv.lock: contains a matching [[package]] entry for every one of the above.",
        "Makefile: no ad hoc unpinned installation of ruff/mypy/pytest/sqlfluff/pre-commit.",
        "CI: no unpinned pip install of a locked tool; runs a locked 'uv sync --locked'; "
        "runs ruff/mypy/pytest/sqlfluff through 'uv run' (directly or via a 'make' target).",
    ]


def main(argv):
    if len(argv) > 2:
        print("usage: test_python_dependencies.py [repo_root]", file=sys.stderr)
        return 2
    if len(argv) == 2:
        repo_root = Path(argv[1])
    else:
        repo_root = Path(__file__).resolve().parents[2]

    try:
        diagnostics = validate(repo_root)
    except DependencyError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        f"PASS: {repo_root} declares and locks its full Python quality toolchain correctly "
        "(single [project] table; exact pins for ruff, mypy, pytest, types-requests, "
        "sqlfluff, pre-commit, and pyyaml; a current uv.lock; and no unpinned installation "
        "of any of these tools in Makefile or CI)."
    )
    for d in diagnostics:
        print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
