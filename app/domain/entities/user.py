from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.entities.enums import UserRole


@dataclass(slots=True)
class UserEntity:
    id: UUID
    name: str
    email: str
    password_hash: str
    role: UserRole
    created_at: datetime
