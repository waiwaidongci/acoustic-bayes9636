from sqlalchemy import Column,Integer,String,Text,ForeignKey,UniqueConstraint,Index
from ..config.database import Base
class SchemaMigration(Base):
    __tablename__='schema_migrations'
    __table_args__=(
        # One active migration per source. MySQL allows multiple NULLs in a
        # unique index, so completed rows release the slot.
        UniqueConstraint('source_id','active_slot',name='uq_schema_migration_active_slot'),
        Index('ix_schema_migrations_source_id','source_id','id'),
    )
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey('acoustic_batches.id'),nullable=False)
    from_version=Column(String(20),nullable=False)
    to_version=Column(String(20),nullable=False)
    state=Column(String(20),nullable=False,default='pending')
    active_slot=Column(Integer,nullable=True)
    sweep_cursor=Column(Integer,nullable=False,default=0)
    note=Column(Text,default='')
    created_by=Column(String(120),default='')
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    completed_at=Column(String(40),default='')
class SchemaMigrationIssue(Base):
    __tablename__='schema_migration_issues'
    __table_args__=(UniqueConstraint('migration_id','record_id',name='uq_migration_issue_record'),)
    id=Column(Integer,primary_key=True)
    migration_id=Column(Integer,ForeignKey('schema_migrations.id'),nullable=False)
    record_id=Column(Integer,ForeignKey('acoustic_samples.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    code=Column(String(40),nullable=False)
    field=Column(String(60),default='')
    detail=Column(Text,nullable=False)
    created_at=Column(String(40),nullable=False)
