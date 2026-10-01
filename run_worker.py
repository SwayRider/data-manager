#!/usr/bin/env python
"""RQ worker entrypoint. Run directly on the host during development,
pointed at the dedicated Redis from infra/data-manager/compose.yaml
(see .env.example) — the same Redis a containerized worker would reach
via its internal service hostname instead.
"""

from rq import Worker

from datamanager.config import config
from datamanager.jobs.queue import queue, redis_conn
from datamanager.logging_setup import configure_logging

if __name__ == "__main__":
    configure_logging(level=config.LOG_LEVEL, json_output=config.LOG_JSON)
    worker = Worker([queue], connection=redis_conn)
    worker.work()
