from app.application.services.auth_service import AuthService
from app.domain.entities.enums import UserRole
from app.domain.entities.user import UserEntity


class RegisterUserUseCase:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    async def execute(self, name: str, email: str, password: str, role: UserRole) -> UserEntity:
        return await self.auth_service.register_user(name=name, email=email, password=password, role=role)
