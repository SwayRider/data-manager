from pathlib import Path

import click
from flask import Flask

from datamanager.countries.autofill import autofill_countries
from datamanager.countries.report import write_countries_md
from datamanager.countries.seed import seed_countries
from datamanager.db import SessionLocal


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-countries")
    def seed_countries_command():
        """Seed/update the country catalog from Natural Earth + curated data."""
        session = SessionLocal()
        try:
            changed = seed_countries(session)
        finally:
            session.close()
        if changed:
            click.echo(f"Seeded/updated {len(changed)} countries: {', '.join(sorted(changed))}")
        else:
            click.echo("No changes — country catalog already up to date.")

    @app.cli.command("evaluate-overlap")
    @click.option("--all", "everything", is_flag=True, help="Re-evaluate every region (default: only never-evaluated ones).")
    def evaluate_overlap_command(everything):
        """(Re)compute the stored auto-detected overlap of regions from their core countries."""
        from datamanager.services import regions as region_service

        session = SessionLocal()
        try:
            count = region_service.evaluate_all_overlap(session, only_missing=not everything)
        finally:
            session.close()
        click.echo(f"Evaluated overlap of {count} region(s).")

    @app.cli.command("autofill-countries")
    @click.option("--no-verify", is_flag=True, help="Skip the live Geofabrik/OpenAddresses checks.")
    @click.option("--refresh", is_flag=True, help="Re-download the provider indexes.")
    def autofill_countries_command(no_verify, refresh):
        """Fill unset Geofabrik paths / OpenAddresses sources from the provider indexes."""
        session = SessionLocal()
        try:
            results = autofill_countries(session, verify=not no_verify, refresh=refresh)
        finally:
            session.close()
        changed = [r for r in results if r.changed]
        failed = [r for r in results if r.geofabrik_error or r.openaddresses_error]
        click.echo(f"Updated {len(changed)} of {len(results)} countries.")
        for r in failed:
            click.echo(f"  ! {r.iso2}: {r.geofabrik_error or ''} {r.openaddresses_error or ''}".rstrip())

    @app.cli.command("export-countries")
    @click.argument("path", default="countries.md", type=click.Path(path_type=Path))
    def export_countries_command(path):
        """Write the country catalog review sheet (default: ./countries.md)."""
        session = SessionLocal()
        try:
            write_countries_md(session, path)
        finally:
            session.close()
        click.echo(f"Wrote {path}")
