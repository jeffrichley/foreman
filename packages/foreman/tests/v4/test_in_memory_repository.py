from foreman.v4.repository import InMemoryTicketRepository

from ._repository_contract import RepositoryContract


class TestInMemory(RepositoryContract):
    factory = InMemoryTicketRepository
