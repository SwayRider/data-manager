import redis
from rq import Queue

from datamanager.config import config

redis_conn = redis.from_url(config.REDIS_URL)
queue = Queue("data-manager", connection=redis_conn)
