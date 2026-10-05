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

    @app.cli.command("package-create")
    @click.option("--config", "config_name", required=True, help="Configuration name, e.g. dev-mini.")
    @click.option("--classes", default="", help="Comma separated subset of tiles,valhalla,pelias,geodata (default: all).")
    @click.option("--label", "labels", multiple=True, help="key=value or bare label; repeatable.")
    @click.option("--note", default="")
    @click.option("--force", is_flag=True, help="Package even when a stage is outdated, unreviewed or running.")
    @click.option("--inline", is_flag=True, help="Run in this process instead of the worker (no Redis needed).")
    def package_create_command(config_name, classes, labels, note, force, inline):
        """Package the approved output of a configuration into the package repository."""
        from datamanager.models import ConfigProfile
        from datamanager.services import runs

        session = SessionLocal()
        profile = session.query(ConfigProfile).filter(ConfigProfile.name == config_name).first()
        if profile is None:
            raise click.ClickException(f"No configuration named {config_name!r}")
        params = {"classes": [c for c in classes.split(",") if c], "note": note, "force": force, "created_by": "cli",
                  "labels": dict((l.partition("=")[0], l.partition("=")[2]) for l in labels)}
        run = runs.create_run(session, "package", profile.id, params=params, triggered_by="cli")
        if inline:
            from datamanager.jobs import tasks

            result = tasks.run_stage(run.id)
            session.refresh(run)
            click.echo(f"Run {run.id}: {result['status']} {(run.report_json or {}).get('summary', {}).get('tag', '')}")
            return
        from datamanager.jobs.queue import queue

        job = queue.enqueue("datamanager.jobs.tasks.run_stage", run.id, job_timeout=12 * 3600)
        runs.set_job_id(session, run, job.id)
        click.echo(f"Run {run.id} queued; follow it on the Build page or with `package-list`.")

    @app.cli.command("package-list")
    def package_list_command():
        """List the packages in the repository index."""
        from datamanager.models import Package

        for p in SessionLocal().query(Package).order_by(Package.id):
            user = ",".join(f"{l.key}={l.value}" if l.value else l.key for l in p.labels if l.origin == "user")
            click.echo(f"{p.tag}  {p.status:9} {p.size_bytes / 1e9:8.1f} GB  {'protected ' if p.protected else ''}{user}")

    @app.cli.command("package-verify")
    @click.argument("tag")
    def package_verify_command(tag):
        """Re-hash every file of a package against its package.json."""
        from datamanager.errors import PackageError
        from datamanager.services import packages

        try:
            problems = packages.verify_package(SessionLocal(), tag)
        except PackageError as exc:
            raise click.ClickException(exc.message)
        if problems:
            for line in problems:
                click.echo(f"  ! {line}")
            raise click.ClickException(f"{tag}: {len(problems)} problem(s)")
        click.echo(f"{tag}: all files match package.json")

    @app.cli.command("packages-reindex")
    def packages_reindex_command():
        """Rebuild the package index from the package.json files in the repository folder."""
        from datamanager.services import packages

        click.echo(str(packages.reindex(SessionLocal())))

    @app.cli.command("packages-prune")
    @click.option("--keep", type=int, default=None, help="Newest unprotected packages to keep (default: setting package.keep).")
    @click.option("--apply", "apply_", is_flag=True, help="Delete; without it only list what would go.")
    def packages_prune_command(keep, apply_):
        """Delete old unprotected packages (dry run unless --apply)."""
        from datamanager.services import packages, settings as settings_service

        session = SessionLocal()
        keep = keep if keep is not None else settings_service.get(session, "package.keep")
        victims = packages.prune(session, keep, dry_run=not apply_)
        click.echo(f"{'Deleted' if apply_ else 'Would delete'}: {', '.join(victims) or 'nothing'}")
