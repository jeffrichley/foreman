from foreman.v4.sqlite_repository import SqliteTicketRepository

from ._repository_contract import RepositoryContract


class TestSqlite(RepositoryContract):
    @staticmethod
    def factory() -> SqliteTicketRepository:
        return SqliteTicketRepository.in_memory()
