from typing import Optional

from pydantic import BaseModel


# 请求模型可以接收密码，因为它只描述进入服务端的数据。
class RegisterRequest(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"
    admin_code: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


# 响应只返回 token、用户名和角色，绝不能把密码或 password_hash 暴露给客户端。
class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class CurrentUserResponse(BaseModel):
    username: str
    role: str
