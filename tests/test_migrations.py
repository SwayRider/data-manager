import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic(*args, env):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_upgrade_creates_tables_and_round_trips(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)

    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "alembic_version", "config_profile", "country", "country_address_source",
        "country_boundary_source", "region", "region_country", "region_overlap",
        "region_openaddresses_exclusion", "country_carve", "style_settings", "global_setting", "region_gtfs_feed",
        "build_run", "build_step", "download_record", "asset",
    }
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()

    # Re-running upgrade head is a no-op.
    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr

    # downgrade -> upgrade round-trips cleanly.
    result = _run_alembic("downgrade", "base", env=env)
    assert result.returncode == 0, result.stderr
    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr


def test_0004_moves_openaddresses_files_into_address_sources(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)
    assert _run_alembic("upgrade", "0003", env=env).returncode == 0

    conn = sqlite3.connect(db_path)
    now = "2026-01-01 00:00:00"
    for iso, files in (("be", '["be/a", "be/b"]'), ("af", "[]")):
        conn.execute(
            "INSERT INTO country VALUES (?, ?, 'x', '[]', NULL, ?, '[]', ?, ?, ?)",
            (iso, iso.upper(), iso, files, now, now),
        )
    conn.commit()
    conn.close()

    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(db_path)
    assert "openaddresses_files_json" not in {r[1] for r in conn.execute("PRAGMA table_info(country)")}
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT country_iso, enabled, config_json FROM country_address_source WHERE source_key='openaddresses'")}
    assert rows == {"be": (1, '{"files": ["be/a", "be/b"]}'), "af": (0, '{"files": []}')}
    conn.close()

    assert _run_alembic("downgrade", "0003", env=env).returncode == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT openaddresses_files_json FROM country WHERE iso2='be'").fetchone()[0] == '["be/a", "be/b"]'
    conn.close()


def test_0008_keeps_override_rows(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)
    assert _run_alembic("upgrade", "0007", env=env).returncode == 0
    conn = sqlite3.connect(db_path)
    now = "2026-01-01 00:00:00"
    conn.execute("INSERT INTO config_profile VALUES (1, 'p', '', ?, ?)", (now, now))
    conn.execute("INSERT INTO region VALUES (1, 1, 'r', '#123456', ?, ?)", (now, now))
    conn.execute("INSERT INTO country VALUES ('be', 'Belgium', 'x', '[]', 'europe/belgium', 'be', '[]', ?, ?)", (now, now))
    conn.execute("INSERT INTO region_overlap_override (region_id, country_iso, mode) VALUES (1, 'be', 'exclude')")
    conn.commit()
    conn.close()

    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT region_id, country_iso, mode FROM region_overlap").fetchall() == [(1, "be", "exclude")]
    assert conn.execute("SELECT overlap_evaluated_at FROM region").fetchone() == (None,)
    conn.close()
