#!/usr/bin/env python3
"""Structural regression test for .pre-commit-config.yaml.

YAML mapping keys must be unique. This repository's config once declared
`repos:` twice at the top level; many YAML parsers resolve duplicate keys
by keeping only the *last* value ("last key wins"), which silently dropped
the first `repos` list (the standard file-safety hooks) from the effective
configuration. This test guards against that regressing, and against a
related mistake: a hook ID configured under a repository that doesn't
actually provide it (e.g. `check-toml`/`check-yaml`, which belong to
`pre-commit/pre-commit-hooks`, being left under an unrelated repo).

Standard library only -- no PyYAML, pre-commit, or network access
required, so this test can run anywhere.

This is not a general YAML parser. It parses just enough of the block
structure (indentation + `key:` / `- item` shapes) to distinguish a
*top-level* mapping key from a same-named key nested somewhere inside a
list item -- so the "is `repos` declared exactly once?" check is a
structural check, not a fragile whole-file `text.count("repos:")`.

Usage:
    python3 scripts/tests/test_pre_commit_config.py [path-to-config.yaml]

With no argument, validates the real repository's
`.pre-commit-config.yaml` (located relative to this script's own path).
An optional path argument lets a negative control point this test at a
scratch fixture instead, without ever touching the real, committed file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# The complete set of hook IDs the effective configuration must retain,
# each exactly once, regardless of which repository entry supplies it.
EXPECTED_HOOK_IDS = [
    "check-added-large-files",
    "end-of-file-fixer",
    "trailing-whitespace",
    "check-merge-conflict",
    "check-toml",
    "check-yaml",
    "sqlfluff-psql-fix",
]

TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*):\s*(.*)$")
REPO_ITEM_RE = re.compile(r"^-\s*repo:\s*(\S+)\s*$")
HOOKS_KEY_RE = re.compile(r"^hooks:\s*$")
HOOK_ID_ITEM_RE = re.compile(r"^-\s*id:\s*(\S+)\s*$")
STAGES_KEY_RE = re.compile(r"^stages:\s*(.*)$")


class ConfigError(Exception):
    """Raised for a structural problem in the pre-commit config."""


def read_lines(path: Path):
    """Return (line_no, indent, content) for meaningful lines.

    Blank lines and full-line comments are dropped. Indentation is the raw
    leading-space count, so callers can compare structural nesting depth
    instead of scanning the file as flat, indentation-blind text.
    """
    lines = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        content = raw.lstrip(" ")
        indent = len(raw) - len(content)
        content = content.rstrip()
        if content.startswith("#"):
            continue
        lines.append((i, indent, content))
    return lines


def find_top_level_keys(lines):
    """Return [(line_no, key_name)] for every mapping key at indent 0.

    Nested keys (e.g. `hooks:` inside a repo entry) are indented and thus
    excluded -- this is what makes duplicate-`repos`-key detection
    structural rather than a flat substring count.
    """
    keys = []
    for line_no, indent, content in lines:
        if indent != 0:
            continue
        m = TOP_KEY_RE.match(content)
        if m:
            keys.append((line_no, m.group(1)))
    return keys


def get_top_level_block(lines, key_line_no):
    """Return the lines belonging to a top-level key's value.

    That is, every line strictly after the key's own line, up to (but not
    including) the next indent-0 line.
    """
    block = []
    started = False
    for line_no, indent, content in lines:
        if line_no == key_line_no:
            started = True
            continue
        if not started:
            continue
        if indent == 0:
            break
        block.append((line_no, indent, content))
    return block


def find_repo_entries(repos_block):
    """Split the repos sequence into one chunk per `- repo: ...` list item.

    Returns (entries, repo_indent) where entries is a list of dicts with
    keys "repo" (the repo URL or "local"), "indent", and "lines" (the
    lines belonging to that entry, up to but excluding the next entry).
    The indent of the first `- repo:` item found sets the expected indent
    for sibling entries in this sequence.
    """
    entries = []
    repo_indent = None
    current = None
    for line_no, indent, content in repos_block:
        m = REPO_ITEM_RE.match(content)
        if m and (repo_indent is None or indent == repo_indent):
            repo_indent = indent
            current = {"repo": m.group(1), "indent": indent, "lines": []}
            entries.append(current)
            continue
        if current is not None:
            current["lines"].append((line_no, indent, content))
    return entries, repo_indent


def find_hook_items(entry_lines, repo_indent):
    """Within one repo entry's lines, return [{"id", "lines"}, ...].

    Looks for a `hooks:` key at the entry's own field indent (repo_indent
    + 2), then collects each `- id: ...` list item beneath it, along with
    the lines belonging to that specific hook item (until the next hook
    item, or a dedent back out of the hooks list).
    """
    field_indent = repo_indent + 2
    hooks_indent = None
    hook_items = []
    current = None
    in_hooks = False
    for line_no, indent, content in entry_lines:
        if not in_hooks:
            if indent == field_indent and HOOKS_KEY_RE.match(content):
                in_hooks = True
            continue
        if indent <= field_indent:
            # Dedented back out of the hooks: block onto a sibling field.
            in_hooks = False
            current = None
            if indent == field_indent and HOOKS_KEY_RE.match(content):
                in_hooks = True
            continue
        m = HOOK_ID_ITEM_RE.match(content)
        if m and (hooks_indent is None or indent == hooks_indent):
            hooks_indent = indent
            current = {"id": m.group(1), "lines": []}
            hook_items.append(current)
            continue
        if current is not None:
            current["lines"].append((line_no, indent, content))
    return hook_items


def hook_is_manual_only(hook_lines):
    """True iff this hook item declares `stages:` containing only 'manual'."""
    for _, _, content in hook_lines:
        m = STAGES_KEY_RE.match(content)
        if not m:
            continue
        value = m.group(1).strip()
        if not value:
            # Block-sequence form (stages:\n  - manual) isn't used by this
            # repo's config; treat as "not confirmed manual-only".
            return False
        inline = value.strip("[]")
        stage_names = [s.strip().strip("'\"") for s in inline.split(",") if s.strip()]
        return stage_names == ["manual"]
    return False


def validate(path: Path):
    """Validate `path` as a pre-commit config. Returns diagnostic strings.

    Raises ConfigError (with a clear, specific message) on any structural
    problem: duplicate/missing top-level `repos` key, a `repos` value that
    isn't a sequence, missing/duplicate hook IDs, a missing or duplicated
    `repo: local` entry, or a non-manual-only sqlfluff-psql-fix hook.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    lines = read_lines(path)
    top_keys = find_top_level_keys(lines)

    repos_occurrences = [ln for ln, k in top_keys if k == "repos"]
    if len(repos_occurrences) == 0:
        raise ConfigError("no top-level 'repos' key found")
    if len(repos_occurrences) > 1:
        raise ConfigError(
            "duplicate top-level 'repos' key found at lines: "
            + ", ".join(str(n) for n in repos_occurrences)
            + " (YAML mapping keys must be unique -- under a 'last key wins' "
            "parser, the later declaration silently replaces the earlier one)"
        )

    repos_line_no = repos_occurrences[0]
    repos_key_content = next(c for ln, _i, c in lines if ln == repos_line_no)
    m = TOP_KEY_RE.match(repos_key_content)
    inline_value = m.group(2).strip() if m else ""
    if inline_value:
        raise ConfigError(
            f"top-level 'repos' key has an inline scalar value ({inline_value!r}); "
            "expected a YAML sequence of repo entries"
        )

    repos_block = get_top_level_block(lines, repos_line_no)
    first_block_line = repos_block[0] if repos_block else None
    if first_block_line is None or not first_block_line[2].startswith("-"):
        raise ConfigError(
            "top-level 'repos' value is not a YAML sequence "
            "(no '- ...' list items found under it)"
        )

    entries, repo_indent = find_repo_entries(repos_block)
    if not entries:
        raise ConfigError("no '- repo: ...' entries found under the top-level 'repos' sequence")

    local_entries = [e for e in entries if e["repo"] == "local"]
    if len(local_entries) == 0:
        raise ConfigError("no 'repo: local' entry found (required for the sqlfluff-psql-fix hook)")
    if len(local_entries) > 1:
        raise ConfigError(f"expected exactly one 'repo: local' entry, found {len(local_entries)}")

    all_hook_ids = []
    sqlfluff_hook_lines = None
    for entry in entries:
        for item in find_hook_items(entry["lines"], entry["indent"]):
            all_hook_ids.append(item["id"])
            if item["id"] == "sqlfluff-psql-fix":
                sqlfluff_hook_lines = item["lines"]

    counts = {}
    for hid in all_hook_ids:
        counts[hid] = counts.get(hid, 0) + 1
    duplicates = {hid: n for hid, n in counts.items() if n > 1}
    if duplicates:
        raise ConfigError(
            "duplicate hook ID(s) found: "
            + ", ".join(f"{hid} (x{n})" for hid, n in sorted(duplicates.items()))
        )

    missing = [h for h in EXPECTED_HOOK_IDS if h not in counts]
    if missing:
        raise ConfigError("missing required hook ID(s): " + ", ".join(missing))

    if sqlfluff_hook_lines is None:
        raise ConfigError("sqlfluff-psql-fix hook not found")
    if not hook_is_manual_only(sqlfluff_hook_lines):
        raise ConfigError(
            "sqlfluff-psql-fix hook is not manual-only (expected 'stages: [manual]')"
        )

    return [
        f"Top-level 'repos' key: exactly one, at line {repos_line_no}.",
        f"Repo entries ({len(entries)}): {', '.join(e['repo'] for e in entries)}.",
        f"'repo: local' entries: {len(local_entries)}.",
        f"Hook IDs found ({len(counts)}): {', '.join(sorted(counts))}.",
        "sqlfluff-psql-fix: manual-only (stages: [manual]).",
    ]


def main(argv):
    if len(argv) > 2:
        print("usage: test_pre_commit_config.py [path-to-.pre-commit-config.yaml]", file=sys.stderr)
        return 2
    if len(argv) == 2:
        path = Path(argv[1])
    else:
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / ".pre-commit-config.yaml"

    try:
        diagnostics = validate(path)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        f"PASS: {path} has exactly one top-level 'repos' key containing every required hook "
        "exactly once,"
    )
    print("      exactly one 'repo: local' entry, and sqlfluff-psql-fix remains manual-only.")
    for d in diagnostics:
        print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
