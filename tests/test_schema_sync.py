"""The JSON Schema and the Python model must not drift apart.

``schema/manifest.schema.json`` is the machine-readable mirror of
:mod:`winmigrate.models`. Nothing enforces that at runtime, so it is enforced
here: adding an enum member without updating the schema fails this test.
"""

import json
from pathlib import Path

import pytest

from winmigrate import manifest as manifest_mod
from winmigrate.models import (
    Action,
    Category,
    Kind,
    RestoreStrategy,
    Sensitivity,
    Severity,
    SkipReason,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "manifest.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def at(schema: dict, *path):
    node = schema
    for key in path:
        node = node[key]
    return node


@pytest.mark.parametrize(
    ("enum_class", "path"),
    [
        (Category, ("$defs", "item", "properties", "category")),
        (Kind, ("$defs", "item", "properties", "kind")),
        (Action, ("$defs", "item", "properties", "action")),
        (Sensitivity, ("$defs", "item", "properties", "sensitivity")),
        (SkipReason, ("$defs", "item", "properties", "skip_reason")),
        (RestoreStrategy, ("$defs", "restoreSpec", "properties", "strategy")),
        (Severity, ("$defs", "note", "properties", "severity")),
    ],
)
def test_schema_enums_match_the_python_model(schema: dict, enum_class, path):
    assert set(at(schema, *path)["enum"]) == {member.value for member in enum_class}


def test_schema_records_the_current_schema_version_shape(schema: dict):
    import re

    assert re.match(schema["properties"]["schema_version"]["pattern"], manifest_mod.SCHEMA_VERSION)


def test_schema_required_keys_match_the_validator(schema: dict):
    assert set(schema["required"]) == set(manifest_mod.REQUIRED_TOP_LEVEL)


def test_digest_algorithms_match_the_hashing_module(schema: dict):
    from winmigrate.util import hashing

    algorithms = set(at(schema, "$defs", "item", "properties", "digest_algo")["enum"])
    assert algorithms == {hashing.FILE_DIGEST_ALGO, hashing.TREE_DIGEST_ALGO}
