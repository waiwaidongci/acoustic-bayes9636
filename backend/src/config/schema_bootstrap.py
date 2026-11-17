"""Idempotent additive schema upgrades for existing deployments.

``Base.metadata.create_all`` creates new tables but never alters existing
ones, so databases created before feature versioning need the new columns
added explicitly.  The statements below are valid for MySQL 8 and SQLite.
"""
from sqlalchemy import inspect,text
ADDITIONS={
    'acoustic_batches': {
        'schema_version': "VARCHAR(20) NOT NULL DEFAULT 'v1'",
    },
    'acoustic_samples': {
        'raw_values': 'JSON',
        'raw_schema_version': 'VARCHAR(20)',
        'schema_version': "VARCHAR(20) NOT NULL DEFAULT 'v1'",
        'converted_values': 'JSON',
        'converted_version': 'VARCHAR(20)',
    },
    'classifier_runs': {
        'schema_version': "VARCHAR(20) NOT NULL DEFAULT 'v1'",
    },
}
INDEXES={
    'ix_records_source_id_id': 'CREATE INDEX ix_records_source_id_id ON acoustic_samples (source_id,id)',
    'ix_records_source_schema_id': 'CREATE INDEX ix_records_source_schema_id ON acoustic_samples (source_id,schema_version,id)',
    'ix_records_source_converted_id': 'CREATE INDEX ix_records_source_converted_id ON acoustic_samples (source_id,converted_version,id)',
}
def ensure_schema(engine):
    inspector=inspect(engine)
    tables=set(inspector.get_table_names())
    with engine.begin() as connection:
        for table,columns in ADDITIONS.items():
            if table not in tables:continue
            existing={c['name'] for c in inspector.get_columns(table)}
            for name,ddl in columns.items():
                if name not in existing:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
        for table in ('acoustic_samples',):
            if table not in tables:continue
            existing={i['name'] for i in inspector.get_indexes(table)}
            for name,ddl in INDEXES.items():
                if name not in existing:
                    try:connection.execute(text(ddl))
                    except Exception:
                        # Concurrent startup or dialect without index support;
                        # a missing optional index must not block the service.
                        pass
