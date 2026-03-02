from fastapi import HTTPException, status

from app.core.security import create_access_token, hash_password, verify_password
from app.domain.entities.enums import UserRole
from app.domain.entities.user import UserEntity
from app.domain.repositories.user_repository import UserRepository


class AuthService:
    def __init__(self, user_repository: UserRepository) -> None:
        self.user_repository = user_repository

    async def register_user(self, name: str, email: str, password: str, role: UserRole) -> UserEntity:
        existing = await self.user_repository.get_by_email(email)
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
        return await self.user_repository.create(
            name=name,
            email=email,
            password_hash=hash_password(password),
            role=role,
        )

    async def login(self, email: str, password: str) -> tuple[UserEntity, str]:
        user = await self.user_repository.get_by_email(email)
        if not user or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        token = create_access_token(subject=str(user.id), extra_claims={"role": user.role.value})
        return user, token
