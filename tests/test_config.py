import json
from pathlib import Path

import pytest

from winmigrate.config import ScanConfig, config_from_dict, load_config_file
from winmigrate.errors import ConfigError


def test_junk_is_excluded_by_name_anywhere():
    config = ScanConfig()
    assert config.is_excluded("Documents/Thumbs.db", "Thumbs.db")
    assert config.is_excluded("Documents/~$report.docx", "~$report.docx")
    assert not config.is_excluded("Documents/report.docx", "report.docx")


def test_path_patterns_are_matched_against_the_profile_relative_path():
    config = ScanConfig()
    assert config.is_excluded("AppData/Local/Temp/x.dat", "x.dat")
    assert not config.is_excluded("Documents/Temp/x.dat", "x.dat")


def test_matching_is_case_insensitive():
    config = ScanConfig()
    assert config.is_excluded("Documents/THUMBS.DB", "THUMBS.DB")
    assert config.is_excluded("appdata/local/temp/x", "x")


def test_regenerable_directories_are_excluded_by_default_and_re_includable():
    default = ScanConfig()
    assert default.is_excluded("Documents/proj/node_modules", "node_modules")
    assert default.is_regenerable("node_modules")

    keeping = ScanConfig(include_regenerable=True)
    assert not keeping.is_excluded("Documents/proj/node_modules", "node_modules")


def test_explicit_include_overrides_an_exclusion():
    config = ScanConfig(extra_includes=("node_modules",))
    assert not config.is_excluded("Documents/proj/node_modules", "node_modules")


def test_extra_excludes_are_applied():
    config = ScanConfig(extra_excludes=("*.iso",))
    assert config.is_excluded("Downloads/big.iso", "big.iso")


def test_config_file_round_trip(tmp_path: Path):
    path = tmp_path / "winmigrate.json"
    path.write_text(json.dumps({"include_regenerable": True, "extra_excludes": ["*.iso"]}), encoding="utf-8")
    config = config_from_dict(load_config_file(path))
    assert config.include_regenerable is True
    assert config.extra_excludes == ("*.iso",)
    assert config.is_excluded("Downloads/a.iso", "a.iso")


def test_unknown_config_keys_are_rejected_rather_than_ignored():
    with pytest.raises(ConfigError, match="unknown config keys: nope"):
        config_from_dict({"nope": 1})


def test_missing_config_file_is_reported_clearly(tmp_path: Path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config_file(tmp_path / "absent.json")


def test_invalid_json_is_reported_clearly(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config_file(path)
