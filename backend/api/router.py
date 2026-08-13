from fastapi import APIRouter
from backend.api.routes.chat import router as chat_router

from backend.api.routes.auth import router as auth_router
from backend.api.routes.sessions import router as sessions_router
from backend.api.routes.documents import router as documents_router

router = APIRouter()
# 聚合路由在本章只挂载认证接口，后续能力要等实现完成后再注册。
router.include_router(auth_router)
router.include_router(sessions_router)
router.include_router(documents_router)

# 聚合顺序保留认证和会话，再加入聊天与文档管理入口。
router.include_router(chat_router)
