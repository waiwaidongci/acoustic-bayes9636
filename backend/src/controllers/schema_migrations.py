from ..services import schema_migrations
class SchemaMigrationsController:
    registry=staticmethod(schema_migrations.registry)
    list=staticmethod(schema_migrations.list_migrations)
    @staticmethod
    def get(db,id):return schema_migrations.migration_view(db,schema_migrations.get_migration(db,id))
    create=staticmethod(schema_migrations.create_migration)
    resume=staticmethod(schema_migrations.resume)
    switch=staticmethod(schema_migrations.switch)
    correct=staticmethod(schema_migrations.correct_record)
