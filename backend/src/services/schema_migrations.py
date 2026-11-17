"""Online feature-schema rollover.

Lifecycle: pending -> running -> ready -> switched.  A run that finds a
sample it cannot convert unambiguously ends blocked.

Conversion does not rewrite the authoritative values.  Each sample keeps its
original observation and its authoritative payload; the rollover materializes
an equivalent payload in the target version into ``converted_values`` in
resumable, keyset-paginated batches (bounded memory).  The final switch is a
single atomic statement that flips the batch version and promotes the
materialized payloads, so studies created before the switch keep reading the
old observation and studies created afterwards read the new one.

Concurrency: the source row is the guard.  Ingestion locks it while deciding
the accepted version; the switch locks it while verifying that every valid
sample is ready.  A sample accepted under the old version can therefore never
land between the final verification and the version flip.
"""
import logging
import os
from sqlalchemy import select,func,update as sql_update,or_
from ..config.database import Session
from ..models.source import Source
from ..models.record import Record
from ..models.schema_migration import SchemaMigration,SchemaMigrationIssue
from ..utils.serialization import now
from .audit import log
from .feature_schema import (
    baseline_version,target_version,version_label,schema_document,
    validate_payload,convert_record_payload,
)
from .queueing import enqueue_schema

BATCH_SIZE=200
ACTIVE_STATES=('pending','running','ready','blocked')


def batch_size():
    try:value=int(os.getenv('MIGRATION_BATCH_SIZE','200'))
    except ValueError:return BATCH_SIZE
    return max(1,min(value,2000))


def registry():return schema_document()


def _issue_text(errors):return '；'.join(error['detail'] for error in errors)[:2000]
def _first_code(errors):return errors[0]['code'] if errors else 'invalid_value'
def _fields(errors):return ','.join(dict.fromkeys(error.get('field','') for error in errors if error.get('field')))[:60]


def active_migration(db,source_id):
    return db.scalar(select(SchemaMigration).where(
        SchemaMigration.source_id==source_id,
        SchemaMigration.active_slot==source_id,
    ).order_by(SchemaMigration.id.desc()))


def allowed_versions(db,source):
    active=active_migration(db,source.id)
    if active:return {active.from_version,active.to_version}
    return {source.schema_version}


def _counts(db,migration):
    rows=db.execute(select(Record.schema_version,func.count()).where(
        Record.source_id==migration.source_id).group_by(Record.schema_version)).all()
    counts={version:int(count) for version,count in rows}
    total=sum(counts.values())
    issue_count=db.scalar(select(func.count()).select_from(SchemaMigrationIssue).where(
        SchemaMigrationIssue.migration_id==migration.id)) or 0
    return counts,total,issue_count


def _materialized_count(db,migration):
    return db.scalar(select(func.count()).select_from(Record).where(
        Record.source_id==migration.source_id,
        or_(Record.schema_version==migration.to_version,
            Record.converted_version==migration.to_version),
    )) or 0


def blocking_reasons(db,migration):
    counts,total,issue_count=_counts(db,migration)
    ready=_materialized_count(db,migration)
    reasons=[]
    if issue_count:
        reasons.append({'code':'quarantined_records',
            'detail':str(issue_count)+' 条样本缺项或口径歧义，已隔离；需修正后才能切换','count':issue_count})
    outstanding=total-issue_count-ready
    if outstanding:
        reasons.append({'code':'pending_conversion',
            'detail':str(outstanding)+' 条样本尚未换算到'+version_label(migration.to_version),'count':outstanding})
    unexpected={version:count for version,count in counts.items()
                if version not in (migration.from_version,migration.to_version)}
    if unexpected:
        reasons.append({'code':'unexpected_version','detail':'存在未知口径的样本：'+'、'.join(unexpected.keys()),
                        'count':sum(unexpected.values())})
    if migration.state=='switched':
        reasons.append({'code':'already_switched','detail':'该批次已完成口径切换','count':0})
    elif migration.state=='blocked' and issue_count==0 and outstanding==0:
        reasons.append({'code':'conversion_incomplete','detail':'换算尚未全部完成，请重新检查','count':0})
    return reasons,counts,total,issue_count,ready


def migration_view(db,migration,include_issues=True):
    reasons,counts,total,issue_count,ready=blocking_reasons(db,migration)
    source=db.get(Source,migration.source_id)
    can_switch=(migration.state in ACTIVE_STATES and migration.active_slot is not None
                and issue_count==0 and ready==total)
    view={
        'id':migration.id,'source_id':migration.source_id,
        'source_name':source.name if source else '',
        'source_version':source.schema_version if source else '',
        'from_version':migration.from_version,'to_version':migration.to_version,
        'from_label':version_label(migration.from_version),'to_label':version_label(migration.to_version),
        'state':migration.state,'created_by':migration.created_by,
        'created_at':migration.created_at,'updated_at':migration.updated_at,
        'completed_at':migration.completed_at,
        'total_records':total,'version_counts':counts,
        'ready_records':ready,'pending_records':max(0,total-issue_count-ready),
        'issue_count':issue_count,'blocking_reasons':reasons,
        'can_switch':can_switch,'active':migration.active_slot is not None,
        'progress':round(ready/(total-issue_count)*100,1) if total>issue_count else 100.0,
    }
    if include_issues:
        view['issues']=[{'record_id':issue.record_id,'reference':issue.reference,
            'code':issue.code,'field':issue.field,'detail':issue.detail,'created_at':issue.created_at}
            for issue in db.scalars(select(SchemaMigrationIssue).where(
                SchemaMigrationIssue.migration_id==migration.id).order_by(
                SchemaMigrationIssue.record_id).limit(200))]
    return view


def list_migrations(db,source_id=None):
    query=select(SchemaMigration).order_by(SchemaMigration.id.desc())
    if source_id is not None:query=query.where(SchemaMigration.source_id==source_id)
    return [migration_view(db,m,include_issues=False) for m in db.scalars(query.limit(50))]


def get_migration(db,id):
    from fastapi import HTTPException
    migration=db.get(SchemaMigration,id)
    if not migration:raise HTTPException(404,'未找到口径迁移')
    return migration


def create_migration(db,user,source_id):
    from fastapi import HTTPException
    source=db.execute(select(Source).where(Source.id==source_id).with_for_update()).scalar_one_or_none()
    if not source:raise HTTPException(404,'未找到来源批次')
    if source.schema_version!=baseline_version():
        raise HTTPException(409,'该批次已是'+version_label(source.schema_version)+'，无需再次迁移')
    existing=active_migration(db,source.id)
    if existing:raise HTTPException(409,'该批次已有进行中的口径迁移 #'+str(existing.id))
    stamp=now()
    migration=SchemaMigration(source_id=source.id,from_version=source.schema_version,
        to_version=target_version(),state='pending',active_slot=source.id,sweep_cursor=0,
        created_by=user.email,created_at=stamp,updated_at=stamp)
    db.add(migration);db.flush()
    log(db,user,'schema.migration.started',migration.id,
        f'{source.name}:{migration.from_version}->{migration.to_version}')
    db.commit()
    _enqueue(migration.id)
    return migration_view(db,migration)


def _enqueue(migration_id):
    try:enqueue_schema(migration_id);return True
    except Exception:logging.exception('schema queue unavailable');return False


def enqueue(migration_id):
    """Public, best-effort wake-up for the schema worker."""
    return _enqueue(migration_id)


def _lock_source(db,source_id):
    return db.execute(select(Source).where(Source.id==source_id).with_for_update()).scalar_one_or_none()


def _issues_by_record(db,migration_id,record_ids):
    if not record_ids:return {}
    rows=db.scalars(select(SchemaMigrationIssue).where(
        SchemaMigrationIssue.migration_id==migration_id,
        SchemaMigrationIssue.record_id.in_(record_ids)))
    return {row.record_id:row for row in rows}


def _quarantined_ids(db,migration):
    return select(SchemaMigrationIssue.record_id).where(SchemaMigrationIssue.migration_id==migration.id)


def _materialize_batch(db,migration,records):
    """Materialize one bounded batch into the target version; caller commits."""
    issues=_issues_by_record(db,migration.id,[r.id for r in records])
    done=0
    for record in records:
        if record.schema_version==migration.to_version:
            # Ingested directly in the new version; nothing to convert.
            if record.id in issues:db.delete(issues[record.id])
            continue
        if record.raw_values is None:
            record.raw_values=dict(record.values or {})
            record.raw_schema_version=record.schema_version
        values,errors=convert_record_payload(record.raw_values,record.values,
            record.raw_schema_version or migration.from_version,migration.to_version)
        if errors:
            issue=issues.get(record.id)
            if issue:
                issue.code=_first_code(errors);issue.field=_fields(errors)
                issue.detail=_issue_text(errors);issue.created_at=now()
            else:
                db.add(SchemaMigrationIssue(migration_id=migration.id,record_id=record.id,
                    reference=record.reference,code=_first_code(errors),
                    field=_fields(errors),detail=_issue_text(errors),created_at=now()))
            record.converted_values=None;record.converted_version=None
            continue
        if record.id in issues:db.delete(issues[record.id])
        record.converted_values=values
        record.converted_version=migration.to_version
        done+=1
    return done


def _outstanding(db,migration):
    target=migration.to_version
    return db.scalar(select(func.count()).select_from(Record).where(
        Record.source_id==migration.source_id,
        Record.schema_version!=target,
        or_(Record.converted_version.is_(None),Record.converted_version!=target),
        Record.id.not_in(_quarantined_ids(db,migration)),
    )) or 0


def _drain(db,migration,max_batches=None):
    """Keyset-paginated materialization. Restart-safe and idempotent."""
    batches=0;restarts=0
    while True:
        records=db.scalars(select(Record).where(
            Record.source_id==migration.source_id,
            Record.id>migration.sweep_cursor,
        ).order_by(Record.id).limit(batch_size())).all()
        if records:
            _materialize_batch(db,migration,records)
            migration.sweep_cursor=records[-1].id
            # Commit per batch so a crash resumes from the cursor, and expire
            # the batch from the identity map: memory stays bounded by one
            # page no matter how many samples the batch holds.
            db.commit()
            db.expunge_all()
            migration=db.get(SchemaMigration,migration.id)
            batches+=1
            if max_batches is not None and batches>=max_batches:return batches
            continue
        if _outstanding(db,migration):
            restarts+=1
            if restarts>2:return batches
            migration.sweep_cursor=0
            batches+=1
            if max_batches is not None and batches>=max_batches:return batches
            continue
        return batches


def advance(migration_id,max_batches=None):
    """Run materialization in resumable batches. Safe to call repeatedly."""
    with Session() as db:
        migration=db.get(SchemaMigration,migration_id)
        if not migration or migration.state in ('switched','cancelled'):return
        migration.state='running';migration.updated_at=now();db.commit()
    while True:
        with Session() as db:
            migration=db.get(SchemaMigration,migration_id)
            if not migration or migration.state in ('switched','cancelled'):return
            _drain(db,migration,max_batches)
            db.commit()
            migration=db.get(SchemaMigration,migration_id)
            counts,total,issue_count=_counts(db,migration)
            ready=_materialized_count(db,migration)
            outstanding=_outstanding(db,migration)
            if outstanding:
                # Interrupted or bounded run; the worker queue/sweep can resume.
                migration.state='running'
            elif issue_count:
                migration.state='blocked'
            else:
                migration.state='ready'
            migration.updated_at=now()
            log(db,'worker','schema.migration.'+migration.state,migration.id,
                f'{ready}/{total} 已换算；隔离 {issue_count}')
            db.commit()
            return


def resume(db,user,migration_id):
    from fastapi import HTTPException
    migration=get_migration(db,migration_id)
    if migration.state=='switched':raise HTTPException(409,'该批次已完成口径切换')
    if migration.active_slot is None:raise HTTPException(409,'该迁移已不再活跃')
    _lock_source(db,migration.source_id)
    db.refresh(migration)
    migration.sweep_cursor=0;migration.state='running';migration.updated_at=now()
    log(db,user,'schema.migration.resumed',migration.id,migration.state)
    db.commit()
    view=migration_view(db,migration)
    view['queued']=_enqueue(migration.id)
    return view


def switch(db,user,migration_id):
    """Atomically flip the batch to the new version.

    Under the source lock, every still-unready sample is materialized once
    more; if any sample is quarantined or cannot be converted the flip is
    refused and nothing changes.
    """
    from fastapi import HTTPException
    migration=get_migration(db,migration_id)
    if migration.state=='switched':raise HTTPException(409,'该批次已完成口径切换')
    _lock_source(db,migration.source_id)
    if migration.active_slot is None:raise HTTPException(409,'该迁移已结束，不能切换')
    # Materialize any tail inside resumable batches. Each batch commits, which
    # releases the lock, so the source is locked again before the flip.
    _drain(db,migration)
    db.commit()
    # Start a fresh transaction whose first statement is the source lock.
    # Under MySQL REPEATABLE READ this pins the verification snapshot after
    # any concurrent ingestion has committed; the lock is then held through
    # the promotion and the version flip.  Nothing may be read before the
    # lock, or the snapshot would predate a concurrent ingestion.
    source_id=migration.source_id
    db.rollback()
    source=_lock_source(db,source_id)
    if not source:raise HTTPException(404,'未找到来源批次')
    migration=get_migration(db,migration_id)
    db.refresh(migration)
    if migration.state=='switched' or migration.active_slot is None:
        raise HTTPException(409,'该迁移已结束，不能切换')
    if source.schema_version!=migration.from_version:
        raise HTTPException(409,'批次口径已被其他操作修改，请刷新后重试')
    _drop_stale_issues(db,migration)
    db.flush()
    counts,total,issue_count=_counts(db,migration)
    ready=_materialized_count(db,migration)
    if issue_count:
        migration.state='blocked';migration.updated_at=now()
        log(db,user,'schema.migration.switch_blocked',migration.id,str(issue_count)+' 条隔离样本')
        db.commit()
        raise HTTPException(409,'仍有 '+str(issue_count)+' 条样本缺项或口径歧义，已隔离，不能切换')
    if ready!=total:
        # A sample arrived after the last batch was committed. Nothing is
        # flipped; re-queue the migration so the new sample is converted.
        migration.state='running';migration.sweep_cursor=0;migration.updated_at=now()
        log(db,user,'schema.migration.switch_blocked',migration.id,str(total-ready)+' 条未换算')
        db.commit()
        queued=_enqueue(migration.id)
        raise HTTPException(409,'仍有 '+str(max(0,total-ready))+' 条样本未完成换算'+
                            ('，已自动重新排队' if queued else '，请稍后重试'))
    stamp=now()
    # Promote materialized payloads and flip the batch version in the same
    # transaction, holding the source lock. Ingestion cannot interleave, so no
    # sample is lost or left in the old version.
    db.execute(sql_update(Record).where(
        Record.source_id==migration.source_id,
        Record.schema_version!=migration.to_version,
        Record.converted_version==migration.to_version,
    ).values(values=Record.converted_values,schema_version=migration.to_version))
    source.schema_version=migration.to_version
    migration.state='switched';migration.active_slot=None
    migration.completed_at=stamp;migration.updated_at=stamp
    log(db,user,'schema.migration.switched',migration.id,
        f'{migration.from_version}->{migration.to_version}；{total} 条样本')
    db.commit()
    return migration_view(db,migration)


def _drop_stale_issues(db,migration):
    issues=db.scalars(select(SchemaMigrationIssue).where(
        SchemaMigrationIssue.migration_id==migration.id)).all()
    for issue in issues:
        record=db.get(Record,issue.record_id)
        if not record or record.schema_version==migration.to_version or \
           record.converted_version==migration.to_version:
            db.delete(issue)

def correct_record(db,user,source_id,record_id,payload,version=None):
    """Fix one quarantined sample so a blocked rollover can proceed.

    Normal ingestion stays append-only; this endpoint exists solely as the
    remedy for a sample isolated by the active migration.
    """
    from fastapi import HTTPException
    source=_lock_source(db,source_id)
    if not source:raise HTTPException(404,'未找到来源批次')
    migration=active_migration(db,source.id)
    if not migration:raise HTTPException(409,'该批次没有进行中的口径迁移，只允许追加样本')
    issue=db.scalar(select(SchemaMigrationIssue).where(
        SchemaMigrationIssue.migration_id==migration.id,
        SchemaMigrationIssue.record_id==record_id))
    if not issue:raise HTTPException(409,'仅可修正本次迁移中已隔离的样本')
    record=db.get(Record,record_id)
    if not record or record.source_id!=source.id:raise HTTPException(404,'未找到样本')
    version=version or migration.to_version
    if version not in (migration.from_version,migration.to_version):
        raise HTTPException(422,'只能按旧口径或新口径提交修正')
    try:values,raw=validate_payload(payload,version)
    except ValueError as exc:raise HTTPException(422,str(exc))
    record.raw_values=raw;record.raw_schema_version=version
    if version==migration.from_version:
        record.values=values;record.schema_version=version;record.converted_values=None
    else:
        record.converted_values=values;record.converted_version=version
    db.delete(issue)
    migration.sweep_cursor=0;migration.state='running';migration.updated_at=now()
    log(db,user,'schema.record.corrected',record.id,record.reference+'@'+version)
    db.commit()
    view=migration_view(db,migration)
    view['queued']=_enqueue(migration.id)
    return view


def requeue_open_migrations():
    """Worker startup: interrupted migrations resume where they left off."""
    with Session() as db:
        rows=db.scalars(select(SchemaMigration).where(
            SchemaMigration.state.in_(('pending','running','ready')))).all()
        ids=[m.id for m in rows]
        for migration in rows:
            if migration.state=='running':
                migration.state='pending';migration.updated_at=now()
        db.commit()
    for id in ids:_enqueue(id)
