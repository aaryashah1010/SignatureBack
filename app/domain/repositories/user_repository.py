from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.entities.user import UserEntity
from app.domain.entities.enums import UserRole


class UserRepository(ABC):
    @abstractmethod
    async def get_by_id(self, user_id: UUID) -> UserEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_email(self, email: str) -> UserEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def create(self, name: str, email: str, password_hash: str, role: UserRole) -> UserEntity:
        raise NotImplementedError

    @abstractmethod
    async def list_signers(self) -> list[UserEntity]:
        raise NotImplementedError
