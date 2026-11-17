from pydantic import BaseModel,Field
class SourceInput(BaseModel):
    name:str=Field(min_length=2,max_length=160)
    filename:str=Field(min_length=3,max_length=160)
    description:str=Field(default='',max_length=2000)
class RecordInput(BaseModel):
    reference:str=Field(min_length=1,max_length=120)
    values:dict
    # Optional per-sample version; defaults to the batch's current version.
    schema_version:str|None=Field(default=None,max_length=20)
class RecordBatch(BaseModel):
    records:list[RecordInput]=Field(min_length=1,max_length=1000)
class RecordCorrection(BaseModel):
    values:dict
    schema_version:str|None=Field(default=None,max_length=20)
class MigrationInput(BaseModel):
    source_id:int
