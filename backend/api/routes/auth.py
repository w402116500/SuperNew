from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import User
from backend.infra.auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_db,
    resolve_role,
)
from backend.infra.passwords import get_password_hash
from backend.schemas import AuthResponse, CurrentUserResponse, LoginRequest, RegisterRequest

router = APIRouter(tags=["auth"])


@router.post("/auth/register", response_model=AuthResponse)
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    # 用户名去掉首尾空白以保持唯一性；密码原文不能 trim，否则会改变用户设置的密码。
    username = (request.username or "").strip()
    password = request.password or ""
    if not username or not password.strip():
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")

    role = resolve_role(request.role, request.admin_code)
    user = User(username=username, password_hash=get_password_hash(password), role=role)
    db.add(user)
    try:
        # 先提交用户记录再签发 token，避免客户端拿到一个数据库中并不存在的身份。
        db.commit()
    # 并发注册可能同时通过预检查；数据库唯一约束冲突时必须回滚并稳定返回 409。
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc

    token = create_access_token(username=username, role=role)
    return AuthResponse(access_token=token, username=username, role=role)


# 用户名不存在和密码错误返回同一提示，避免泄露哪些账号已经注册。
@router.post("/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(username=user.username, role=user.role)
    return AuthResponse(access_token=token, username=user.username, role=user.role)


# 当前用户来自认证依赖，接口不接受客户端自行提交 username。
@router.get("/auth/me", response_model=CurrentUserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return CurrentUserResponse(username=current_user.username, role=current_user.role)
