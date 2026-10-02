import os
import stat

import pytest

from datamanager import tools as registry
from datamanager.errors import ValidationError
from datamanager.services import settings as settings_service
from datamanager.services import tools as svc


@pytest.fixture(autouse=True)
def fresh_cache():
    svc.clear_cache()
    yield
    svc.clear_cache()


def _script(path, output, exit_code=0):
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    tool = registry.ToolDef("faketool", "Fake tool", "tests", ("faketool-xyz",), min_version=(2, 0), apt="faketool")
    monkeypatch.setattr(svc, "TOOLS", (tool,))
    monkeypatch.setattr(svc, "BY_KEY", {"faketool": tool})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    return tool, bindir


def test_missing_then_detected_after_install(db_session, fake):
    _, bindir = fake
    assert svc.detect(db_session, "faketool").status == "missing"
    _script(bindir / "faketool-xyz", "faketool version 2.3.1")
    assert svc.detect(db_session, "faketool").status == "missing"  # cached
    status = svc.detect(db_session, "faketool", force=True)  # the re-detect button
    assert status.status == "ok" and status.version == "2.3.1" and status.path.endswith("faketool-xyz")


def test_outdated_and_error(db_session, fake):
    _, bindir = fake
    _script(bindir / "faketool-xyz", "faketool version 1.9")
    assert svc.detect(db_session, "faketool").status == "outdated"
    _script(bindir / "faketool-xyz", "boom", exit_code=3)
    assert svc.detect(db_session, "faketool", force=True).status == "error"


def test_override_wins_and_invalid_override_is_error(db_session, fake, tmp_path):
    _, bindir = fake
    _script(bindir / "faketool-xyz", "faketool version 1.0")
    custom = _script(tmp_path / "custom", "custom 3.4")
    status = svc.set_path(db_session, "faketool", custom)
    assert status.status == "ok" and status.path == custom and status.version == "3.4" and status.override == custom

    assert svc.set_path(db_session, "faketool", str(tmp_path / "nope")).status == "error"
    plain = tmp_path / "plain"
    plain.write_text("x")
    assert "not executable" in svc.set_path(db_session, "faketool", str(plain)).message

    assert svc.set_path(db_session, "faketool", "").status == "outdated"  # cleared: PATH binary again
    assert settings_service.get_raw(db_session, "toolpath.faketool") is None


def test_unknown_tool_rejected(db_session, fake):
    with pytest.raises(ValidationError):
        svc.set_path(db_session, "nope", "/bin/true")


def test_file_tool_uses_data_root_default(db_session, tmp_data_root, monkeypatch):
    tool = registry.ToolDef("jarx", "Jar", "tests", kind="file", default_file="tools/x.jar", required=False)
    monkeypatch.setattr(svc, "TOOLS", (tool,))
    monkeypatch.setattr(svc, "BY_KEY", {"jarx": tool})
    assert svc.detect(db_session, "jarx").status == "missing"
    (tmp_data_root / "tools").mkdir()
    (tmp_data_root / "tools" / "x.jar").write_text("jar")
    assert svc.detect(db_session, "jarx", force=True).status == "ok"


def test_problems_split_required_and_optional(db_session, monkeypatch):
    req = registry.ToolDef("r", "Req", "t", ("nonexistent-r",))
    opt = registry.ToolDef("o", "Opt", "t", ("nonexistent-o",), required=False)
    monkeypatch.setattr(svc, "TOOLS", (req, opt))
    monkeypatch.setattr(svc, "BY_KEY", {"r": req, "o": opt})
    assert svc.problems(db_session) == {"required": ["Req"], "optional": ["Opt"]}


def test_registry_is_complete():
    keys = [t.key for t in registry.TOOLS]
    assert len(keys) == len(set(keys))
    for t in registry.TOOLS:
        assert t.apt or t.install_help, f"{t.key} has no install hint"
        assert t.kind in ("file", "built", "library") or t.binaries


def test_library_tool_reports_missing_data_files(db_session, tmp_path, monkeypatch):
    tool = registry.ToolDef("libfake", "libfake", "tests", kind="library", pkg_config="libfake",
                            data_dirs=(str(tmp_path / "share"),), data_marker="models")
    monkeypatch.setattr(svc, "TOOLS", (tool,))
    monkeypatch.setattr(svc, "BY_KEY", {"libfake": tool})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    assert svc.detect(db_session, "libfake").status == "missing"  # no pkg-config, no ldconfig on this PATH
    _script(bindir / "pkg-config", "1.1.0")
    status = svc.detect(db_session, "libfake", force=True)
    assert status.status == "error" and status.version == "1.1.0" and "data files" in status.message
    (tmp_path / "share" / "models").mkdir(parents=True)
    assert svc.detect(db_session, "libfake", force=True).status == "ok"
    with pytest.raises(ValidationError):
        svc.set_path(db_session, "libfake", "/usr/lib/x.so")


def test_libpostal_is_a_required_tool_with_install_help():
    tool = registry.BY_KEY["libpostal"]
    assert tool.required and tool.kind == "library" and "bootstrap.sh" in tool.install_help and "ldconfig" in tool.install_help
