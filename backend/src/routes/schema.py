from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.source import MigrationInput,RecordCorrection
from ..services.auth import current_user,writer,admin
from ..controllers.schema_migrations import SchemaMigrationsController as C
router=APIRouter()
@router.get('/schema/registry')
def registry(u=Depends(current_user)):return C.registry()
@router.get('/schema/migrations')
def migrations(source_id:int|None=None,db=Depends(db_session),u=Depends(current_user)):return C.list(db,source_id)
@router.get('/schema/migrations/{id}')
def detail(id:int,db=Depends(db_session),u=Depends(current_user)):return C.get(db,id)
@router.post('/schema/migrations')
def create(data:MigrationInput,db=Depends(db_session),u=Depends(writer)):return C.create(db,u,data.source_id)
@router.post('/schema/migrations/{id}/resume')
def resume(id:int,db=Depends(db_session),u=Depends(writer)):return C.resume(db,u,id)
@router.post('/schema/migrations/{id}/switch')
def switch(id:int,db=Depends(db_session),u=Depends(admin)):return C.switch(db,u,id)
@router.put('/sources/{source_id}/records/{record_id}/correction')
def correct(source_id:int,record_id:int,data:RecordCorrection,db=Depends(db_session),u=Depends(writer)):
    return C.correct(db,u,source_id,record_id,data.values,data.schema_version)
