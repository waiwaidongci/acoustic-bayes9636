import secrets
from fastapi import HTTPException
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from ..models.source import Source
from ..models.record import Record
from ..utils.serialization import row,now
from .audit import log
from .domain_validation import validate_record
from ..config.settings import CONFIG
from . import feature_schema
from . import schema_migrations


def versioned():
    return 'feature_schemas' in CONFIG


def source_row(source,record_count=None):
    data=row(source)
    if not versioned():
        data.pop('schema_version',None)
    if record_count is not None:data['record_count']=record_count
    return data


def list_sources(db):
    rows=[]
    for s in db.scalars(select(Source).order_by(Source.id)):
        d=source_row(s)
        d['record_count']=db.scalar(select(func.count()).select_from(Record).where(Record.source_id==s.id))
        rows.append(d)
    return rows


def source_or_404(db,id):
    s=db.get(Source,id)
    if not s:raise HTTPException(404,'未找到来源批次')
    return s


def _lock_source(db,id):
    return db.execute(select(Source).where(Source.id==id).with_for_update()).scalar_one_or_none()


def save_source(db,user,data,id=None):
    if id:
        s=_lock_source(db,id)
        if not s:raise HTTPException(404,'未找到来源批次')
    else:
        s=Source(key='user-'+secrets.token_hex(10),schema_version=feature_schema.baseline_version())
    for k,v in data.model_dump().items():setattr(s,k,v)
    db.add(s);db.flush();log(db,user,'source.updated' if id else 'source.created',s.id,s.name);db.commit();return source_row(s)


def _record_row(record):
    data=row(record)
    if not versioned():
        for key in ('schema_version','raw_schema_version'):data.pop(key,None)
    # converted_values is an internal staging area for the rollover; it is not
    # the sample's authoritative value and is not exposed.
    data.pop('converted_values',None);data.pop('converted_version',None)
    return data


def list_records(db,id):
    source_or_404(db,id)
    return [_record_row(x) for x in db.scalars(select(Record).where(Record.source_id==id).order_by(Record.id))]


def add_records(db,user,id,data):
    """Append samples, accepting whichever version is current for the batch.

    The source row is locked for the duration so the final schema switch can
    never interleave with an ingestion: an accepted sample is always stored in
    the version that is authoritative at commit time.
    """
    source=_lock_source(db,id)
    if not source:raise HTTPException(404,'未找到来源批次')
    allowed=schema_migrations.allowed_versions(db,source) if versioned() else {None}
    active=schema_migrations.active_migration(db,source.id) if versioned() else None
    default_version=active.to_version if active else source.schema_version
    validated=[];refs=set()
    for r in data.records:
        if r.reference in refs:raise HTTPException(422,'批内样本编号重复')
        refs.add(r.reference)
        requested=getattr(r,'schema_version',None) or default_version
        if requested not in allowed:
            raise HTTPException(422,'当前批次只接受口径 '+'、'.join(sorted(x for x in allowed if x)))
        try:
            values,raw=validate_record(r.values,requested)
        except ValueError as e:raise HTTPException(422,str(e))
        if db.scalar(select(Record.id).where(Record.source_id==id,Record.reference==r.reference)):raise HTTPException(409,'样本编号已存在：'+r.reference)
        validated.append(Record(source_id=id,reference=r.reference,values=values,
                                raw_values=raw,
                                raw_schema_version=requested if raw else None,
                                schema_version=requested or 'v1'))
    db.add_all(validated)
    try:db.flush()
    except IntegrityError:raise HTTPException(409,'样本编号已存在，并发写入请重试')
    log(db,user,'records.added',id,str(len(validated)))
    # A sample reported in the old version during a rollover must be converted
    # before the switch; wake the migration instead of waiting for the switch
    # to notice it.
    requeue=None
    if active and any(r.schema_version==active.from_version for r in validated):
        if active.state in ('ready','blocked'):
            active.state='running';active.updated_at=now()
            requeue=active.id
    db.commit()
    if requeue is not None:schema_migrations.enqueue(requeue)
    return {'added':len(validated)}
