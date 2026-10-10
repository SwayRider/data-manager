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

    @app.cli.command("deploy-config-save")
    @click.argument("key")
    @click.option("--file", "path", required=True, type=click.Path(exists=True, dir_okay=False), help="JSON file with the configuration.")
    @click.option("--description", default="")
    def deploy_config_save_command(key, path, description):
        """Create or replace a deploy configuration (no secrets in it: only environment variable names)."""
        import json

        from datamanager.deploy import orchestrator
        from datamanager.errors import DeployError

        try:
            row = orchestrator.save_config(SessionLocal(), key, json.loads(Path(path).read_text()), description)
        except (DeployError, ValueError) as exc:
            raise click.ClickException(getattr(exc, "message", str(exc)))
        click.echo(f"Saved deploy configuration {row.key} ({row.driver})")

    @app.cli.command("deploy-config-list")
    def deploy_config_list_command():
        """List the deploy configurations."""
        from datamanager.models import DeployConfig

        for row in SessionLocal().query(DeployConfig).order_by(DeployConfig.key):
            click.echo(f"{row.key}  {row.driver}  classes: {', '.join(row.config_json.get('classes', {}))}  {row.description}")

    def _deploy_error(exc):
        return click.ClickException(getattr(exc, "message", str(exc)))

    @app.cli.command("deploy-plan")
    @click.option("--config", "config_key", required=True, help="Deploy configuration key, e.g. dev-mini.")
    @click.option("--tag", default=None, help="Package tag or unique label (default: the newest verified package).")
    @click.option("--classes", default="", help="Comma separated subset (default: all configured).")
    @click.option("--drop-previous", is_flag=True, help="Plan as if the previous release were removed first.")
    def deploy_plan_command(config_key, tag, classes, drop_previous):
        """Show what a deploy would do; changes nothing."""
        from datamanager.deploy import orchestrator
        from datamanager.errors import DeployError

        try:
            the_plan = orchestrator.plan(SessionLocal(), config_key, tag, [c for c in classes.split(",") if c], drop_previous=drop_previous)
        except DeployError as exc:
            raise _deploy_error(exc)
        click.echo(f"Deploy {the_plan['package']} to {the_plan['config']}")
        for entry in the_plan["classes"]:
            what = f"skip ({entry['skip']})" if entry["skip"] else f"{entry['bytes_to_copy'] / 1e9:8.1f} GB to copy"
            click.echo(f"  {entry['class']:9} {entry['parts']:4} part(s) {entry['bytes'] / 1e9:8.1f} GB  {what}  "
                       f"current={entry['current']} previous={entry['previous']}"
                       + (f"  REMOVES previous {entry['drops_previous']} first" if entry.get("drops_previous") else ""))
        for line in the_plan["warnings"]:
            click.echo(f"  ~ {line}")
        for line in the_plan["problems"]:
            click.echo(f"  ! {line}")
        if the_plan["problems"]:
            raise click.ClickException("Not deployable as planned")

    @app.cli.command("deploy")
    @click.option("--config", "config_key", required=True)
    @click.option("--tag", default=None, help="Package tag or unique label (default: the newest verified package).")
    @click.option("--classes", default="", help="Comma separated subset (default: all configured).")
    @click.option("--drop-previous", is_flag=True, help="Remove the previous release of each class BEFORE copying, to save space "
                  "(no rollback target until this deploy is healthy).")
    @click.option("--inline", is_flag=True, help="Run in this process instead of the worker (no Redis needed).")
    def deploy_command(config_key, tag, classes, drop_previous, inline):
        """Deploy a package to an environment, class by class (geodata, valhalla, pelias, tiles)."""
        from datamanager.deploy import orchestrator
        from datamanager.errors import DeployError
        from datamanager.services import runs

        session = SessionLocal()
        wanted = [c for c in classes.split(",") if c]
        try:
            the_plan = orchestrator.plan(session, config_key, tag, wanted, drop_previous=drop_previous)
            if the_plan["problems"]:
                raise DeployError("; ".join(the_plan["problems"]))
            package = orchestrator.resolve_package(session, tag)
        except DeployError as exc:
            raise _deploy_error(exc)
        params = {"deploy_config": config_key, "tag": package.tag, "classes": wanted, "triggered_by": "cli",
                  "drop_previous": drop_previous}
        run = runs.create_run(session, "deploy", package.config_profile_id, params=params, triggered_by="cli")
        _start_deploy_run(session, run, inline)

    @app.cli.command("deploy-rollback")
    @click.option("--config", "config_key", required=True)
    @click.option("--classes", default="")
    @click.option("--inline", is_flag=True)
    def deploy_rollback_command(config_key, classes, inline):
        """Put `current` back to `previous` on the target; the release that was rolled back is removed from it."""
        from datamanager.deploy import orchestrator
        from datamanager.errors import DeployError
        from datamanager.services import runs

        session = SessionLocal()
        try:
            current = orchestrator.state(session, config_key)
            wanted = [c for c in classes.split(",") if c] or list(current)
            missing = [c for c in wanted if not current.get(c, {}).get("previous")]
            if missing:
                raise DeployError(f"No previous release to roll back to for: {', '.join(missing)}")
        except DeployError as exc:
            raise _deploy_error(exc)
        params = {"deploy_config": config_key, "classes": wanted, "rollback": True, "triggered_by": "cli"}
        run = runs.create_run(session, "deploy", None, params=params, triggered_by="cli")
        _start_deploy_run(session, run, inline)

    def _start_deploy_run(session, run, inline):
        from datamanager.services import runs

        if inline:
            from datamanager.jobs import tasks

            result = tasks.run_stage(run.id)
            session.refresh(run)
            click.echo(f"Run {run.id}: {result['status']}")
            summary = (run.report_json or {}).get("summary", {})
            for name, r in summary.get("classes", {}).items():
                click.echo(f"  {name:9} {r.get('status')}  current={r.get('current')}  {r.get('error', r.get('reason', ''))}")
            if run.status != "awaiting_review" and result["status"] != "success" and (run.report_json or {}).get("error"):
                raise click.ClickException(run.report_json["error"])
            return
        from datamanager.jobs.queue import queue

        job = queue.enqueue("datamanager.jobs.tasks.run_stage", run.id, job_timeout=12 * 3600)
        runs.set_job_id(session, run, job.id)
        click.echo(f"Run {run.id} queued; follow it with `deploy-state` or on its run page.")

    @app.cli.command("deploy-state")
    @click.option("--config", "config_key", required=True)
    def deploy_state_command(config_key):
        """What is on the target now (read from the target) and the last deployments."""
        from datamanager.deploy import orchestrator
        from datamanager.errors import DeployError

        session = SessionLocal()
        try:
            for name, entry in orchestrator.state(session, config_key).items():
                click.echo(f"{name:9} current={entry['current']} previous={entry['previous']} releases={','.join(entry['releases'])}"
                           + (f" partial={','.join(entry['partial'])}" if entry["partial"] else ""))
            for d in orchestrator.history(session, config_key, 10):
                click.echo(f"  deployment {d.id:3} {d.status:11} {d.package_tag:14} {'rollback ' if d.detail_json.get('rollback') else ''}"
                           f"{','.join(d.classes_json)}  {d.started_at:%Y-%m-%d %H:%M}")
        except DeployError as exc:
            raise _deploy_error(exc)

    @app.cli.command("cleanup")
    @click.argument("tag")
    @click.option("--category", "categories", multiple=True, help="Category key; repeatable (default: those ticked in Settings).")
    @click.option("--apply", "apply_", is_flag=True, help="Execute; without it only list what would go.")
    def cleanup_command(tag, categories, apply_):
        """Free SSD space after packaging (needs a verified package; dry run unless --apply)."""
        from datamanager.errors import PackageError
        from datamanager.services import cleanup

        session = SessionLocal()
        try:
            chosen = list(categories) or [k for k, on in cleanup.defaults(session).items() if on]
            the_plan = cleanup.plan(session, tag, chosen)
            for key, entry in the_plan.by_category().items():
                if key in chosen:
                    click.echo(f"{key:18} {entry['count']:4} item(s) {entry['bytes'] / 1e9:9.1f} GB  skipped {len(entry['skipped'])}")
                    for item in entry["skipped"]:
                        click.echo(f"    - {item.label}: {item.skip}")
            if not apply_:
                click.echo("Dry run; add --apply to delete.")
                return
            result = cleanup.apply(session, tag, chosen)
        except PackageError as exc:
            raise click.ClickException(exc.message)
        click.echo(f"Freed {result.freed / 1e9:.1f} GB: purged {result.purged}, deleted {result.deleted}, skipped {len(result.skipped)}")
        for line in result.errors:
            click.echo(f"  ! {line}")
