from sqlalchemy import select
from fastapi import HTTPException
from ..config.settings import CONFIG
from ..models.run import Run
from ..utils.serialization import row,now
from .sources import source_or_404,source_row,list_records
from .domain_validation import validate_parameters
from .audit import log
from . import feature_schema
from .queueing import enqueue_analysis


def get_run(db,id):
    r=db.get(Run,id)
    if not r:raise HTTPException(404,'未找到分析任务')
    return r


def create_run(db,user,data):
    s=source_or_404(db,data.source_id);records=list_records(db,s.id)
    try:params=validate_parameters(data.parameters)
    except ValueError as e:raise HTTPException(422,str(e))
    version=getattr(s,'schema_version',None)
    if 'feature_schemas' in CONFIG and version:
        requested=getattr(data,'schema_version',None)
        if requested:
            if requested not in {r.get('schema_version') for r in records}:
                raise HTTPException(409,'该批次没有'+feature_schema.version_label(requested)+'样本')
            # Explicit version: freeze only that version's samples.  This is how
            # a study stays possible during a rollover, without mixing units.
            records=[r for r in records if r.get('schema_version')==requested]
            version=requested
        else:
            # A study is frozen in exactly one schema version.  During a
            # rollover the batch can legitimately hold both versions; such a
            # mixed snapshot is refused instead of silently trained.  Studies
            # created before the rollover keep their own snapshot untouched.
            versions={r.get('schema_version') for r in records}
            if versions!={version}:
                extra=sorted(x for x in versions if x and x!=version)
                raise HTTPException(409,'该批次仍有'+('、'.join(feature_schema.version_label(x) for x in extra))+
                                    '样本待统一口径；可指定口径建立分版研究，或待切换完成后再建立研究')
    if len(records)<3:raise HTTPException(422,'至少录入3条样本后才能执行')
    r=Run(source_id=s.id,name=data.name,parameters=params,
          snapshot={'source':source_row(s),'records':records},
          schema_version=version or feature_schema.baseline_version(),created_at=now(),state='queued')
    db.add(r);db.flush();log(db,user,'run.queued',r.id,data.name);db.commit()
    try:enqueue_analysis(r.id)
    except Exception:
        r.state='failed';r.error='队列暂不可用，请重新创建任务';db.commit();raise HTTPException(503,r.error)
    return row(r)


def list_runs(db):
    return [{k:v for k,v in row(r).items() if k not in ('snapshot','result')} for r in db.scalars(select(Run).order_by(Run.id.desc()))]
