"""Concurrency, interruption and scale invariants for the rollover."""
import os, sys, tracemalloc
from pathlib import Path
import fakeredis
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def boot(tmp_path, monkeypatch, batch=50):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('MIGRATION_BATCH_SIZE', str(batch))
    monkeypatch.setenv('SEED_DEMO', 'false')
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6399/0')
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('JWT_SECRET', 'test')
    fake = fakeredis.FakeRedis()
    import redis as real_redis
    monkeypatch.setattr(real_redis.Redis, 'from_url', staticmethod(lambda *a, **k: fake))
    for name in list(sys.modules):
        if name == 'src' or name.startswith('src.'):
            del sys.modules[name]
    import src.main as main
    from src.config.database import Base, engine, Session
    from src.config.schema_bootstrap import ensure_schema
    from src.models.user import User
    from src.models import schema_migration
    from src.utils.security import hash_password
    Base.metadata.create_all(engine)
    ensure_schema(engine)
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@acoustic-bayes.demo',
                        password_hash=hash_password('Research2026!'), role=role))
        db.commit()
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    token = client.post('/auth/login', json={
        'email': 'admin@acoustic-bayes.demo', 'password': 'Research2026!'}).json()['token']
    return client, {'Authorization': 'Bearer ' + token}, Session


def old(i):
    return {'rms': 2.0 + i * 0.001, 'centroid': 1200 + i,
            'kurtosis': 3.2, 'label': ['正常', '轴承磨损', '转子偏心'][i % 3]}


def new(i):
    return {'rms': 2.0 + i * 0.001, 'centroid': (1200 + i) / 1000.0,
            'kurtosis': 3.2, 'label': ['正常', '轴承磨损', '转子偏心'][i % 3]}


def bulk(client, headers, values_list, version=None, chunk=500):
    source = client.post('/sources', headers=headers,
                         json={'name': '批量', 'filename': 'f.csv', 'description': ''}).json()
    for start in range(0, len(values_list), chunk):
        records = []
        for offset, values in enumerate(values_list[start:start + chunk]):
            item = {'reference': f'R-{start + offset:06}', 'values': values}
            if version:
                item['schema_version'] = version
            records.append(item)
        response = client.post(f"/sources/{source['id']}/records", headers=headers,
                               json={'records': records})
        assert response.status_code == 200, response.text
    return source


def test_sample_arriving_after_sweep_is_not_lost(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch)
    source = bulk(client, headers, [old(i) for i in range(20)])
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid)
    assert client.get(f'/schema/migrations/{mid}', headers=headers).json()['state'] == 'ready'
    # A sample accepted after the sweep finished, still under the old version.
    late = client.post(f"/sources/{source['id']}/records", headers=headers,
                       json={'records': [{'reference': 'LATE-1', 'values': old(99),
                                          'schema_version': 'v1'}]})
    assert late.status_code == 200, late.text
    response = client.post(f'/schema/migrations/{mid}/switch', headers=headers)
    assert response.status_code == 200, response.text
    records = client.get(f"/sources/{source['id']}/records", headers=headers).json()
    assert len(records) == 21
    assert {r['reference']: round(r['values']['centroid'], 6) for r in records}['LATE-1'] == 1.299
    assert all(r['schema_version'] == 'v2' for r in records)


def test_unconvertible_late_sample_blocks_instead_of_mixing(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch)
    source = bulk(client, headers, [old(i) for i in range(6)])
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid)
    from src.models.record import Record
    with Session() as db:
        db.add(Record(source_id=source['id'], reference='LATE-BAD', schema_version='v1',
                      values={'rms': 1.0, 'centroid': 1000.0, 'kurtosis': 2.0},
                      raw_values={'rms': 1.0, 'centroid': 1000.0, 'kurtosis': 2.0},
                      raw_schema_version='v1'))
        db.commit()
    response = client.post(f'/schema/migrations/{mid}/switch', headers=headers)
    assert response.status_code == 409
    version = client.get('/sources', headers=headers).json()[0]['schema_version']
    assert version == 'v1'
    view = client.get(f'/schema/migrations/{mid}', headers=headers).json()
    assert view['state'] == 'blocked'
    assert any(x['reference'] == 'LATE-BAD' for x in view['issues'])


def test_ingestion_rejects_stale_version_after_switch(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch)
    source = bulk(client, headers, [old(i) for i in range(4)])
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid)
    assert client.post(f'/schema/migrations/{mid}/switch', headers=headers).status_code == 200
    stale = client.post(f"/sources/{source['id']}/records", headers=headers,
                        json={'records': [{'reference': 'OLD-1', 'values': old(50),
                                           'schema_version': 'v1'}]})
    assert stale.status_code == 422
    fresh = client.post(f"/sources/{source['id']}/records", headers=headers,
                        json={'records': [{'reference': 'NEW-1', 'values': new(50),
                                           'schema_version': 'v2'}]})
    assert fresh.status_code == 200, fresh.text
    # a second rollover is not offered once the batch is new-version
    again = client.post('/schema/migrations', headers=headers, json={'source_id': source['id']})
    assert again.status_code == 409


def test_two_hundred_thousand_samples_bounded_memory(tmp_path, monkeypatch):
    """Keyset batches keep working-set memory independent of batch size."""
    client, headers, Session = boot(tmp_path, monkeypatch, batch=500)
    import json
    import src.config.database as database
    from src.models.source import Source
    from src.models.record import Record
    # Insert directly: HTTP batching of 200k rows would dominate the test.
    with Session() as db:
        source = Source(key='big', name='大批次', filename='big.csv', schema_version='v1')
        db.add(source)
        db.flush()
        payload = {'rms': 2.0, 'centroid': 1200.0, 'kurtosis': 3.2, 'label': '正常'}
        db.bulk_insert_mappings(Record, [
            {'source_id': source.id, 'reference': f'B-{i:07}', 'values': payload,
             'raw_values': payload, 'raw_schema_version': 'v1', 'schema_version': 'v1'}
            for i in range(200_000)])
        db.commit()
        source_id = source.id
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source_id}).json()['id']
    from src.services import schema_migrations
    seen = []
    original = schema_migrations._materialize_batch
    def spy(db, migration, records):
        seen.append(len(records))
        return original(db, migration, records)
    monkeypatch.setattr(schema_migrations, '_materialize_batch', spy)
    tracemalloc.start()
    schema_migrations.advance(mid)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert max(seen) <= 500
    assert len(seen) >= 400            # 200k rows / 500 per page
    # Peak stays proportional to one page, not to the 200k rows.
    assert peak < 40 * 1024 * 1024, peak
    assert client.post(f'/schema/migrations/{mid}/switch', headers=headers).status_code == 200
    counts = client.get('/sources', headers=headers).json()
    assert counts[0]['record_count'] == 200_000


def test_worker_restart_resumes_from_cursor(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch, batch=3)
    source = bulk(client, headers, [old(i) for i in range(11)])
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid, max_batches=1)
    with Session() as db:
        migration = db.get(schema_migrations.SchemaMigration, mid)
        cursor = migration.sweep_cursor
    assert cursor > 0
    # Simulated crash: worker restarts and requeues interrupted migrations.
    schema_migrations.requeue_open_migrations()
    with Session() as db:
        migration = db.get(schema_migrations.SchemaMigration, mid)
        assert migration.state == 'pending'
        assert migration.sweep_cursor == cursor
    schema_migrations.advance(mid)
    view = client.get(f'/schema/migrations/{mid}', headers=headers).json()
    assert view['state'] == 'ready' and view['ready_records'] == 11
    assert max(v['values']['centroid'] for v in
               client.get(f"/sources/{source['id']}/records", headers=headers).json()) > 1000


def test_replaying_a_batch_twice_is_idempotent(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch)
    source = bulk(client, headers, [old(i) for i in range(8)])
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid)
    with Session() as db:
        migration = db.get(schema_migrations.SchemaMigration, mid)
        migration.converted_values = None
        db.query(schema_migrations.SchemaMigration).filter(
            schema_migrations.SchemaMigration.id == mid).update({'sweep_cursor': 0})
        db.query(__import__('src.models.record', fromlist=['Record']).Record).filter(
            __import__('src.models.record', fromlist=['Record']).Record.source_id == source['id']
        ).update({'converted_values': None, 'converted_version': None}, synchronize_session=False)
        db.commit()
    schema_migrations.advance(mid)
    assert client.post(f'/schema/migrations/{mid}/switch', headers=headers).status_code == 200
    records = client.get(f"/sources/{source['id']}/records", headers=headers).json()
    assert [round(r['values']['centroid'], 6) for r in records] == \
           [round((1200 + i) / 1000, 6) for i in range(8)]


def test_issue_cleanup_when_sample_is_also_new_version(tmp_path, monkeypatch):
    client, headers, Session = boot(tmp_path, monkeypatch)
    source = bulk(client, headers, [old(i) for i in range(5)])
    from src.models.record import Record
    with Session() as db:
        row = db.query(Record).filter(Record.source_id == source['id']).first()
        row.raw_values = {'rms': 1.0, 'kurtosis': 2.0}
        db.commit()
        broken = row.id
    mid = client.post('/schema/migrations', headers=headers,
                      json={'source_id': source['id']}).json()['id']
    from src.services import schema_migrations
    schema_migrations.advance(mid)
    assert client.get(f'/schema/migrations/{mid}', headers=headers).json()['issue_count'] == 1
    # Correcting under the new version should clear the issue and allow switching.
    fix = client.put(f"/sources/{source['id']}/records/{broken}/correction",
                     headers=headers, json={'values': new(1), 'schema_version': 'v2'})
    assert fix.status_code == 200, fix.text
    schema_migrations.advance(mid)
    assert client.post(f'/schema/migrations/{mid}/switch', headers=headers).status_code == 200
