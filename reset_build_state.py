"""One-off: reset build state (runs, steps, download records, assets) and stale RQ jobs.

Backs up the SQLite DB first (backup API, safe for WAL). Configuration is not touched.
Run: PYTHONPATH=. .venv/bin/python reset_build_state.py
"""
import os
import sqlite3

db = "/mnt/ssd2/swayrider/data/state/db.sqlite3"
backup = db + ".bak-prefresh"
src, dst = sqlite3.connect(db), sqlite3.connect(backup)
src.backup(dst)
dst.close()
src.close()
print("backup:", backup, os.path.getsize(backup), "bytes")

try:
    from redis import Redis
    from rq import Queue
    from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry
    from rq.worker import Worker

    from datamanager.config import config

    redis = Redis.from_url(config.REDIS_URL)
    queue = Queue("data-manager", connection=redis)
    for name, registry in (
        ("started", StartedJobRegistry(queue=queue)),
        ("failed", FailedJobRegistry(queue=queue)),
        ("finished", FinishedJobRegistry(queue=queue)),
    ):
        ids = registry.get_job_ids()
        print("registry", name, len(ids))
        for job_id in ids:
            registry.remove(job_id, delete_job=True)
    print("queued jobs removed:", queue.count)
    queue.empty()
    for worker in Worker.all(connection=redis):
        print("deregistering worker", worker.name, worker.state)
        worker.register_death()
except Exception as exc:  # Redis not reachable: the DB reset below still matters
    print("redis step skipped:", type(exc).__name__, exc)

tables = ("asset", "download_record", "build_step", "build_run")  # children before parents
con = sqlite3.connect(db)
with con:
    for table in tables:
        con.execute(f"delete from {table}")
print({t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in tables})
con.close()
