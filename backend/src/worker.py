import json,time,logging
from sqlalchemy import select
from .config.database import Session
from .config.settings import ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit,schema_migration
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
from .services.queueing import queue,ANALYSIS_QUEUE,SCHEMA_QUEUE
from .services import schema_migrations
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        if not r or r.state!='queued':return
        r.state='running';db.commit()
        try:
            result=calculate(r.snapshot['records'],r.parameters)
            json.dumps(result,allow_nan=False)
            r.result=result;r.state='completed'
            response=csv_response(result,'run.csv')
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id)
        except Exception as e:
            logging.exception('analysis failed');r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error)
        db.commit()
def execute_schema(id):
    try:schema_migrations.advance(id)
    except Exception:logging.exception('schema migration %s failed',id)
def main():
    try:schema_migrations.requeue_open_migrations()
    except Exception:logging.exception('could not requeue schema migrations')
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        value=queue.blpop([SCHEMA_QUEUE,ANALYSIS_QUEUE],timeout=2)
        if value:
            kind=value[0].decode() if isinstance(value[0],bytes) else value[0]
            id=int(value[1])
            execute_schema(id) if kind==SCHEMA_QUEUE else execute(id)
if __name__=='__main__':main()
