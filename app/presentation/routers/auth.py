from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.application.services.auth_service import AuthService
from app.application.use_cases.login_user import LoginUserUseCase
from app.application.use_cases.register_user import RegisterUserUseCase
from app.core.dependencies import get_auth_service
from app.presentation.controllers.schemas import AuthResponse, LoginRequest, RegisterRequest, UserResponse


router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserResponse:
    use_case = RegisterUserUseCase(auth_service)
    user = await use_case.execute(
        name=payload.name,
        email=payload.email,
        password=payload.password,
        role=payload.role,
    )
    return UserResponse.model_validate(user)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthResponse:
    use_case = LoginUserUseCase(auth_service)
    user, token = await use_case.execute(email=payload.email, password=payload.password)
    return AuthResponse(access_token=token, user=UserResponse.model_validate(user))
