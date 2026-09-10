import json
from pathlib import Path

import sys

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
    assert json.loads(plan_path.read_text(encoding="utf-8"))["items"]
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
    config.write_text(json.dumps({"include_regenerable": True}), encoding="utf-8")
    main(["scan", "--profile-root", str(profile), "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    documents = next(i for i in payload["items"] if i["id"] == "files:documents")
    assert documents["size_bytes"] == 95_500


def test_a_bad_config_file_exits_with_an_error_not_a_traceback(profile: Path, tmp_path: Path, capsys):
    config = tmp_path / "conf.json"
    config.write_text("{oops", encoding="utf-8")
    code = main(["scan", "--profile-root", str(profile), "--config", str(config)])
    assert code == 2
    assert "error:" in capsys.readouterr().out


def test_log_file_is_written_when_asked(profile: Path, tmp_path: Path):
    log_path = tmp_path / "run.log"
    run(["--quiet", "-v"], profile, extra=["--log-file", str(log_path)])
    assert "sync root" in log_path.read_text(encoding="utf-8")


def test_require_windows_refuses_without_the_development_override(monkeypatch):
    # The guard's whole job is to refuse on a non-Windows host, so the test has
    # to be one. On a Windows runner it correctly does not raise, which is the
    # opposite of what is being asserted here.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WINMIGRATE_ALLOW_NON_WINDOWS", raising=False)
    with pytest.raises(PlatformError, match="runs on Windows"):
        require_windows()


def test_the_preview_json_is_not_redacted(profile: Path, registry: dict, capsys, monkeypatch):
    """--json shows the user their own machine, so it is not a sidecar.

    Reported when `scan --json | findstr counts` printed nothing: the software
    record was being run through the bundle's public view.
    """
    from winmigrate.scan import software as software_mod

    def fake_scan(env):
        inventory = software_mod.SoftwareInventory()
        inventory.entries = [
            software_mod.SoftwareEntry(name="ACME Bespoke Suite", version="3.2"),
            software_mod.SoftwareEntry(
                name="Mozilla Firefox", version="128", winget_id="Mozilla.Firefox"
            ),
        ]
        return inventory

    monkeypatch.setattr(software_mod, "scan_software", fake_scan)
    main(["scan", "--profile-root", str(profile), "--json"])
    payload = json.loads(capsys.readouterr().out)

    item = next(i for i in payload["items"] if i["category"] == "software")
    assert "record" in item, "the preview must not withhold the user's own inventory"
    assert item["record"]["counts"]["total"] == 2
    assert "record_withheld" not in item


def test_no_software_skips_the_inventory(profile: Path, capsys, monkeypatch):
    from winmigrate.scan import software as software_mod

    def explode(env):
        raise AssertionError("--no-software must not run the inventory")

    monkeypatch.setattr(software_mod, "scan_software", explode)
    assert main(["scan", "--profile-root", str(profile), "--json", "--no-software"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not any(i["category"] == "software" for i in payload["items"])


def test_json_output_survives_a_console_that_cannot_encode_it(profile: Path, monkeypatch, capsys):
    """rich's Windows console writer raises on characters outside its code page.

    Reported by `scan --json | findstr ...` dying with UnicodeEncodeError.
    Machine-readable output must not be styled or routed through that path.
    """
    import io

    from winmigrate import cli as cli_mod

    class Cp1252Stream(io.TextIOBase):
        encoding = "cp1252"

        def __init__(self):
            self.chunks: list[str] = []

        def write(self, text):
            text.encode("cp1252")  # raises exactly as the Windows console does
            self.chunks.append(text)
            return len(text)

    stream = Cp1252Stream()
    monkeypatch.setattr(cli_mod.sys, "stdout", stream)
    cli_mod.emit_json({"applications": ["Café ☕", "北京 app"]})

    written = "".join(stream.chunks)
    assert written.isascii(), "must fall back to escaped JSON rather than raising"
    assert json.loads(written)["applications"] == ["Café ☕", "北京 app"]


def test_json_output_is_not_styled(profile: Path, capsys):
    """It goes to another program; syntax highlighting would corrupt it."""
    main(["scan", "--profile-root", str(profile), "--json", "--no-software"])
    out = capsys.readouterr().out
    assert "\x1b[" not in out, "ANSI styling must not reach machine-readable output"
    assert json.loads(out)["schema_version"]
