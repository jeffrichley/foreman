import pytest

from foreman.v4.postgres_repository import PostgresTicketRepository

from ._repository_contract import RepositoryContract
from .postgres_fixture import (  # noqa: F401  (fixture imports)
    clean_postgres_dsn,
    postgres_dsn,
)


class TestPostgres(RepositoryContract):
    @pytest.fixture()
    def repo(self, clean_postgres_dsn):  # noqa: F811
        return PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
