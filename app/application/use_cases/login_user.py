from app.application.services.auth_service import AuthService
from app.domain.entities.user import UserEntity


class LoginUserUseCase:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    async def execute(self, email: str, password: str) -> tuple[UserEntity, str]:
        return await self.auth_service.login(email=email, password=password)
