from redis import Redis
from ..config.settings import REDIS_URL
queue=Redis.from_url(REDIS_URL)
ANALYSIS_QUEUE='analysis:queue'
SCHEMA_QUEUE='schema:queue'
def enqueue_analysis(id):queue.rpush(ANALYSIS_QUEUE,id)
def enqueue_schema(id):queue.rpush(SCHEMA_QUEUE,id)
