"""Offline tests for the feature-schema rollover O, not the worker."""
import importlib,os,sys
from pathlib import Path
import fakeredis
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))


def fresh_env(tmp_path,monkeypatch,batch=2):
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'db.sqlite'))
    monkeypatch.setenv('MIGRATION_BATCH_SIZE',str(batch))
    monkeypatch.setenv('SEED_DEMO','false')
    monkeypatch.setenv('REDIS_URL','redis://127.0.0.1:6399/0')
    monkeypatch.setenv('ARTIFACT_DIR',str(tmp_path/'artifacts'))
    monkeypatch.setenv('JWT_SECRET','test')
    fake=fakeredis.FakeRedis()
    import redis as real_redis
    monkeypatch.setattr(real_redis.Redis,'from_url',staticmethod(lambda *a,**k:fake))
    for name in list(sys.modules):
        if name=='src' or name.startswith('src.'):
            del sys.modules[name]
    import src.main as main
    from src.config.database import Base,engine,Session
    from src.config.schema_bootstrap import ensure_schema
    Base.metadata.create_all(engine);ensure_schema(engine)
    from src.models.user import User
    from src.models import schema_migration
    from src.utils.security import hash_password
    with Session() as db:
        for role in ('admin','analyst','viewer'):
            db.add(User(email=role+'@acoustic-bayes.demo',password_hash=hash_password('Research2026!'),role=role))
        db.commit()
    return main.app,Session


def api(monkeypatch,tmp_path,batch=2):
    app,Session=fresh_env(tmp_path,monkeypatch,batch)
    from fastapi.testclient import TestClient
    client=TestClient(app)
    token=client.post('/auth/login',json={'email':'admin@acoustic-bayes.demo','password':'Research2026!'}).json()['token']
    headers={'Authorization':'Bearer '+token}
    return client,headers,Session


def source_with(client,headers,values_list,version=None):
    source=client.post('/sources',headers=headers,json={'name':'批次','filename':'f.csv','description':''}).json()
    records=[]
    for i,values in enumerate(values_list):
        item={'reference':f'R-{i:04}','values':values}
        if version:item['schema_version']=version
        records.append(item)
    response=client.post(f"/sources/{source['id']}/records",headers=headers,json={'records':records})
    assert response.status_code==200,response.text
    return source


def old(i):
    return {'rms':2.0+i*0.01,'centroid':1200+i,'kurtosis':3.2,'label':['正常','轴承磨损','转子偏心'][i%3]}


def new(i):
    return {'rms':2.0+i*0.01,'centroid':(1200+i)/1000.0,'kurtosis':3.2,'label':['正常','轴承磨损','转子偏心'][i%3]}


def run_worker(client,headers,migration_id):
    from src.services import schema_migrations
    schema_migrations.advance(migration_id)


def test_registry_and_aliases(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path)
    registry=client.get('/schema/registry',headers=headers).json()
    assert registry['baseline']=='v1' and registry['target']=='v2'
    assert registry['versions']['v2']['fields']['centroid']['unit']=='kHz'
    assert registry['versions']['v2']['fields']['centroid']['base_factor']==1000.0
    # alias accepted in the old version, reported in kHz
    source=source_with(client,headers,[{'rms':1,'centroid_khz':1.25,'kurtosis':2,'label':'正常'}],version='v1')
    records=client.get(f"/sources/{source['id']}/records",headers=headers).json()
    assert records[0]['values']['centroid']==1250.0
    # missing required field is rejected at ingestion
    bad=client.post(f"/sources/{source['id']}/records",headers=headers,
                    json={'records':[{'reference':'X','values':{'rms':1,'kurtosis':2,'label':'正常'}}]})
    assert bad.status_code==422 and '频谱质心' in bad.json()['detail']
    # ambiguous aliases are rejected
    ambiguous=client.post(f"/sources/{source['id']}/records",headers=headers,
                    json={'records':[{'reference':'Y','values':{'rms':1,'centroid':1.2,'centroid_hz':1200,'kurtosis':2,'label':'正常'}}]})
    assert ambiguous.status_code==422 and '歧义' in ambiguous.json()['detail']


def test_migration_converts_and_switches(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path,batch=3)
    source=source_with(client,headers,[old(i) for i in range(10)])
    migration=client.post('/schema/migrations',headers=headers,json={'source_id':source['id']})
    assert migration.status_code==200,migration.text
    mid=migration.json()['id']
    run_worker(client,headers,mid)
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    assert view['state']=='ready' and view['can_switch'] and view['ready_records']==10
    # original values still old-version while migration is ready
    records=client.get(f"/sources/{source['id']}/records",headers=headers).json()
    assert {r['values']['centroid'] for r in records}=={1200+i for i in range(10)}
    switched=client.post(f'/schema/migrations/{mid}/switch',headers=headers)
    assert switched.status_code==200,switched.text
    assert switched.json()['state']=='switched'
    assert client.get('/sources',headers=headers).json()[0]['schema_version']=='v2'
    records=client.get(f"/sources/{source['id']}/records",headers=headers).json()
    assert all(r['schema_version']=='v2' for r in records)
    assert {round(r['values']['centroid'],6) for r in records}=={round((1200+i)/1000,6) for i in range(10)}


def test_study_rules_around_switch(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path)
    source=source_with(client,headers,[old(i) for i in range(12)])
    migration=client.post('/schema/migrations',headers=headers,json={'source_id':source['id']}).json()
    # old-version study allowed before the migration starts affecting data
    params={'folds':2,'smoothing':1e-9}
    old_run=client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'迁移前研究','parameters':params})
    assert old_run.status_code==200,old_run.text
    # during migration no study can be frozen from a mixed batch
    mid=migration['id']
    client.post(f'/sources/{source["id"]}/records',headers=headers,
                json={'records':[{'reference':'NEW-1','values':new(20),'schema_version':'v2'}]})
    mixed=client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'混版研究','parameters':params})
    assert mixed.status_code==409 and '口径' in mixed.json()['detail']
    # an explicit version keeps research possible during the rollover
    old_pinned=client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'旧口径研究','parameters':params,'schema_version':'v1'})
    assert old_pinned.status_code==200,old_pinned.text
    assert old_pinned.json()['schema_version']=='v1'
    assert len(old_pinned.json()['snapshot']['records'])==12
    run_worker(client,headers,mid)
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    # still mixed: old rows converted but not promoted; version_counts keeps both
    assert view['version_counts'].get('v1',0)>=12
    assert client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'混版研究2','parameters':params}).status_code==409
    r=client.post(f'/schema/migrations/{mid}/switch',headers=headers)
    assert r.status_code==200,r.text
    new_run=client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'切换后研究','parameters':params})
    assert new_run.status_code==200,new_run.text
    assert new_run.json()['schema_version']=='v2'
    assert all(r['schema_version']=='v2' for r in new_run.json()['snapshot']['records'])
    # historical run snapshot untouched
    detail=client.get(f"/runs/{old_run.json()['id']}",headers=headers).json()
    assert detail['schema_version']=='v1'
    assert {round(x['values']['centroid'],3) for x in detail['snapshot']['records']}=={round(1200+i,3) for i in range(12)}


def test_blocking_and_correction(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path)
    values=[old(i) for i in range(6)]
    source=source_with(client,headers,values)
    from src.models.record import Record
    with Session() as db:
        row=db.query(Record).filter(Record.source_id==source['id']).first()
        row.values={'rms':1.0,'centroid':1000.0,'kurtosis':2.5,'label':'正常'}
        row.raw_values={'rms':1.0,'centroid':1000.0,'kurtosis':2.5}
        db.commit()
        broken_id=row.id
    mid=client.post('/schema/migrations',headers=headers,json={'source_id':source['id']}).json()['id']
    run_worker(client,headers,mid)
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    assert view['state']=='blocked' and not view['can_switch']
    assert any(x['code']=='quarantined_records' for x in view['blocking_reasons'])
    assert view['issues'][0]['code']=='missing_field'
    blocked=client.post(f'/schema/migrations/{mid}/switch',headers=headers)
    assert blocked.status_code==409
    # correct under the new version
    fixed=client.put(f"/sources/{source['id']}/records/{broken_id}/correction",headers=headers,
                     json={'values':{'rms':1.0,'centroid':1.0,'kurtosis':2.5,'label':'正常'},'schema_version':'v2'})
    assert fixed.status_code==200,fixed.text
    run_worker(client,headers,mid)
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    assert view['state']=='ready' and view['can_switch']
    assert client.post(f'/schema/migrations/{mid}/switch',headers=headers).status_code==200


def test_resume_and_idempotent_retry(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path,batch=2)
    source=source_with(client,headers,[old(i) for i in range(9)])
    mid=client.post('/schema/migrations',headers=headers,json={'source_id':source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid,max_batches=2)
    # interruption mid-way
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    assert view['state']=='running' and view['ready_records']==4
    schema_migrations.advance(mid)
    view=client.get(f'/schema/migrations/{mid}',headers=headers).json()
    assert view['state']=='ready'
    # retries do not double-convert
    schema_migrations.advance(mid);schema_migrations.advance(mid)
    assert client.post(f'/schema/migrations/{mid}/switch',headers=headers).status_code==200
    after=[r['values']['centroid'] for r in client.get(f"/sources/{source['id']}/records",headers=headers).json()]
    assert all(v<10 for v in after)
    assert [round(v,6) for v in after]==[round((1200+i)/1000,6) for i in range(9)]


def test_viewer_cannot_start_or_switch(monkeypatch,tmp_path):
    client,headers,Session=api(monkeypatch,tmp_path)
    source=source_with(client,headers,[old(i) for i in range(3)])
    viewer=client.post('/auth/login',json={'email':'viewer@acoustic-bayes.demo','password':'Research2026!'}).json()['token']
    vh={'Authorization':'Bearer '+viewer}
    assert client.post('/schema/migrations',headers=vh,json={'source_id':source['id']}).status_code==403
    mid=client.post('/schema/migrations',headers=headers,json={'source_id':source['id']}).json()['id']
    assert client.post(f'/schema/migrations/{mid}/switch',headers=vh).status_code==403
