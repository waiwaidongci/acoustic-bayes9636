from sqlalchemy import Column,Integer,String,JSON,ForeignKey,UniqueConstraint,Index
from ..config.database import Base
from ..config.settings import CONFIG
class Record(Base):
    __tablename__=CONFIG['record_table']
    __table_args__=(
        UniqueConstraint('source_id','reference',name='uq_record_reference'),
        Index('ix_records_source_id_id','source_id','id'),
    )
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    # Authoritative payload in the unit declared by ``schema_version``.  It is
    # never rewritten by a rollover, so studies created before the switch keep
    # reading exactly the values that were observed.
    values=Column(JSON,nullable=False)
    schema_version=Column(String(20),nullable=False,default='v1')
    # Exact payload as submitted, including aliases and reported units.  The
    # original observation is always reproducible from here.
    raw_values=Column(JSON,nullable=True)
    raw_schema_version=Column(String(20),nullable=True)
    # Offline materialization of this sample in the migration target version,
    # filled in resumable batches.  The switch flips only the batch version;
    # it never rewrites a million rows in one transaction.
    converted_values=Column(JSON,nullable=True)
    converted_version=Column(String(20),nullable=True)
