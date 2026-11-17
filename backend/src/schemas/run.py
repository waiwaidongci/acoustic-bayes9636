from pydantic import BaseModel,Field
class RunInput(BaseModel):
    source_id:int
    name:str=Field(min_length=2,max_length=160)
    parameters:dict
    # Optional: freeze the study on one version of the batch during a rollover.
    schema_version:str|None=Field(default=None,max_length=20)
