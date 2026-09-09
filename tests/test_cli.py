import json
from pathlib import Path

import pytest
from conftest import snapshot

from winmigrate.cli import main
from winmigrate.errors import PlatformError
from winmigrate.platform_win import require_windows


def run(argv, profile: Path, extra=()):
    return main(["scan", "--profile-root", str(profile), *extra, *argv])


def test_scan_prints_a_preview_and_exits_zero(profile: Path, capsys):
    assert run([], profile) == 0
    out = capsys.readouterr().out
    assert "scan preview" in out
    assert "Documents" in out
    assert "read-only scan" in out


def test_scan_json_emits_the_public_manifest(profile: Path, capsys):
    assert run(["--json"], profile) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"]
    assert payload["mode"] == "full"
    ids = {item["id"] for item in payload["items"]}
    assert "files:documents" in ids
    assert payload["totals"]["capture_bytes"] > 0


def test_save_plan_writes_json_and_nothing_else(profile: Path, tmp_path: Path):
    plan_path = tmp_path / "plans" / "plan.json"
    before = snapshot(profile)
    assert run(["--save-plan", str(plan_path), "--quiet"], profile) == 0
    assert json.loads(plan_path.read_text())["items"]
    assert snapshot(profile) == before, "scan must not touch the profile it inspects"


def test_exclude_and_include_flags_reach_the_scan(profile: Path, capsys):
    run(["--json", "--exclude", "*.docx"], profile)
    excluded = json.loads(capsys.readouterr().out)
    documents = next(i for i in excluded["items"] if i["id"] == "files:documents")
    assert documents["size_bytes"] == 500  # notes.txt + main.py; report.docx dropped

    run(["--json", "--include", "node_modules"], profile)
    included = json.loads(capsys.readouterr().out)
    documents = next(i for i in included["items"] if i["id"] == "files:documents")
    assert documents["size_bytes"] == 95_500


def test_files_only_mode_is_recorded_in_the_plan(profile: Path, capsys):
    run(["--json", "--files-only"], profile)
    assert json.loads(capsys.readouterr().out)["mode"] == "files-only"


def test_config_file_options_are_applied(profile: Path, tmp_path: Path, capsys):
    config = tmp_path / "conf.json"
    config.write_text(json.dumps({"include_regenerable": True}))
    main(["scan", "--profile-root", str(profile), "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    documents = next(i for i in payload["items"] if i["id"] == "files:documents")
    assert documents["size_bytes"] == 95_500


def test_a_bad_config_file_exits_with_an_error_not_a_traceback(profile: Path, tmp_path: Path, capsys):
    config = tmp_path / "conf.json"
    config.write_text("{oops")
    code = main(["scan", "--profile-root", str(profile), "--config", str(config)])
    assert code == 2
    assert "error:" in capsys.readouterr().out


def test_log_file_is_written_when_asked(profile: Path, tmp_path: Path):
    log_path = tmp_path / "run.log"
    run(["--quiet", "-v"], profile, extra=["--log-file", str(log_path)])
    assert "sync root" in log_path.read_text()


def test_require_windows_refuses_without_the_development_override(monkeypatch):
    monkeypatch.delenv("WINMIGRATE_ALLOW_NON_WINDOWS", raising=False)
    with pytest.raises(PlatformError, match="runs on Windows"):
        require_windows()
