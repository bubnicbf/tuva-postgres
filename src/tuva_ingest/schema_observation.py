"""Deterministic, PHI-free schema drift observation for one source
record (see docs/SOURCE_CONTRACT.md "Schema observation" and
migrations/007_object_storage_raw_contract.sql for the
`schema_observation` table this feeds).

Walks a decoded JSON object recursively and records exactly two things
per field path: the path itself, and the observed JSON *type name* --
never the value. Behavior is fully deterministic so the same logical
shape always produces the same fingerprint, and two different shapes
always produce different fingerprints (tested explicitly in
tests/unit/test_schema_observation.py).

Path/type rules:
  object field       dotted path, e.g. "address.city"
  array               the path itself gets type "array"; every element
                      is additionally walked at the SAME path with a
                      trailing "[]" segment (e.g. "diagnoses[].code"),
                      so an array of objects contributes its elements'
                      field paths once, not once per array index (index
                      position is never part of a path -- arrays of any
                      length produce the same observation set).
  null                type name "null" -- recorded like any other type,
                      never skipped and never merged with a non-null
                      observation of the same path (a path that is
                      sometimes null and sometimes a string legitimately
                      produces two type observations for that one path;
                      that IS the drift signal this table exists to
                      capture).
  mixed-type field    the field path simply accumulates more than one
                      type name across records/runs (see `merge_paths`)
                      -- this module never "picks a winner"; that
                      decision, if ever needed, belongs to a human
                      reviewing `schema_observation`, not to silent
                      coercion here.
  scalar (string/
  number/boolean)     the path gets that JSON type's name directly
                      ("string", "number", "boolean").
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"  # pragma: no cover - decoded JSON never produces this


def walk_paths(value: Any, *, prefix: str = "$") -> dict[str, set[str]]:
    """Return `{field_path: {observed type names}}` for `value` (a
    decoded JSON object or array), rooted at `prefix` (default `"$"`,
    the whole-record root -- never included as its own entry; only
    field paths beneath it are recorded)."""
    observations: dict[str, set[str]] = {}
    _walk(value, prefix, observations)
    return observations


def _record(observations: dict[str, set[str]], path: str, type_name: str) -> None:
    observations.setdefault(path, set()).add(type_name)


def _walk(value: Any, path: str, observations: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        if path != "$":
            _record(observations, path, "object")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path != "$" else key
            _walk_scalar_or_container(child, child_path, observations)
    elif isinstance(value, list):
        if path != "$":
            _record(observations, path, "array")
        element_path = f"{path}[]"
        for element in value:
            _walk_scalar_or_container(element, element_path, observations)
    # A bare scalar/None at the root ($) contributes no field-path
    # observations -- every managed endpoint's records are JSON objects,
    # never bare scalars, so this is a defensive no-op, not a real code
    # path in practice.


def _walk_scalar_or_container(value: Any, path: str, observations: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        _walk(value, path, observations)
    elif isinstance(value, list):
        _walk(value, path, observations)
    else:
        _record(observations, path, _json_type_name(value))


def merge_paths(*observation_sets: dict[str, set[str]]) -> dict[str, set[str]]:
    """Union several `walk_paths` results together (e.g. across every
    record on a page) into one combined `{path: {types}}` mapping."""
    merged: dict[str, set[str]] = {}
    for observations in observation_sets:
        for path, types in observations.items():
            merged.setdefault(path, set()).update(types)
    return merged


def fingerprint(observations: dict[str, set[str]]) -> str:
    """A deterministic SHA-256 fingerprint over the sorted
    path/type representation: order-independent (the same set of
    path/type pairs always produces the same fingerprint regardless of
    dict/set iteration order or the order records were observed in) and
    sensitive to any real shape change (an added/removed path, or a
    path gaining/losing an observed type, always changes the
    fingerprint)."""
    canonical = sorted((path, sorted(types)) for path, types in observations.items())
    body = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def observe_record(value: Any) -> tuple[str, dict[str, set[str]]]:
    """Convenience wrapper: `walk_paths` a single record and return
    `(fingerprint, observations)` together."""
    observations = walk_paths(value)
    return fingerprint(observations), observations
