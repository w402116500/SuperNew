# logging 用于在服务端记录错误和完整堆栈，便于排查线上问题。
import logging

# APIRouter 用于注册接口；Depends 用于注入当前用户；HTTPException 用于返回 HTTP 错误。
from fastapi import APIRouter, Depends, HTTPException

# storage 封装 PostgreSQL 和 Redis 的会话读写逻辑。
from backend.chat.storage import storage
# User 是当前已认证用户对应的数据库模型。
from backend.db.models import User
# get_current_user 会验证 JWT，并返回当前用户；无效 Token 会直接返回 401。
from backend.infra.auth import get_current_user
# 这些 Pydantic 模型定义接口响应的字段和数据格式。
from backend.schemas import (
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)

# 创建“会话管理”接口的路由对象，Swagger 文档中会归入 sessions 分组。
router = APIRouter(tags=["sessions"])
# __name__ 是当前模块名；用它创建日志记录器便于日志来源定位。
logger = logging.getLogger(__name__)


# session_id 来自路径，但用户身份只能来自已验证 token。
# {session_id} 是路径参数，例如 GET /sessions/python-study 中的 "python-study"。
# response_model 会将返回结果校验并序列化为 SessionMessagesResponse 格式。
@router.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
async def get_session_messages(session_id: str, current_user: User = Depends(get_current_user)):
    """读取当前用户指定会话的全部消息。"""
    # Depends(get_current_user) 会从 Authorization 请求头验证 Token，
    # 然后自动把已认证用户对象赋给 current_user。
    try:
        # 存储层返回普通字典列表；列表推导式将每个字典转换为 MessageInfo 响应模型。
        messages = [
            {
                # 将原始字典交给外层响应模型一次性校验，避免热重载后旧 MessageInfo 类实例失配。
                "type": msg["type"],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
                "rag_trace": msg.get("rag_trace"),
            }
            # 传入用户名和会话 ID，存储层会再次校验该会话是否属于当前用户。
            for msg in storage.get_session_messages(current_user.username, session_id)
        ]
        # 使用外层响应模型包装消息列表，最终返回 JSON：{"messages": [...]}。
        return SessionMessagesResponse(messages=messages)
    except Exception as exc:
        # logger.exception 会记录错误信息和完整 Python 调用栈。
        logger.exception("读取会话消息失败")
        # 对客户端隐藏内部数据库/缓存细节，只返回通用 500 错误。
        # from exc 保留原始异常的关联，方便在日志或调试器中追踪原因。
        raise HTTPException(status_code=500, detail="读取会话消息失败") from exc


# 注册 GET /sessions 接口，用于获取当前用户的会话摘要列表。
@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(current_user: User = Depends(get_current_user)):
    """返回当前用户的全部会话摘要，不包含每个会话的完整消息内容。"""
    try:
        # storage 返回字典列表。**item 是字典解包，等价于逐个传入 session_id、title 等参数。
        sessions = [SessionInfo(**item) for item in storage.list_session_infos(current_user.username)]
        # 列表在返回前按更新时间倒序排列，让最近使用的会话排在最前。
        # lambda x: x.updated_at 表示“用每个 SessionInfo 的 updated_at 字段作为排序依据”。
        sessions.sort(key=lambda x: x.updated_at, reverse=True)
        # 使用外层响应模型包装列表，最终返回 JSON：{"sessions": [...]}。
        return SessionListResponse(sessions=sessions)
    except Exception as exc:
        logger.exception("读取会话列表失败")
        raise HTTPException(status_code=500, detail="读取会话列表失败") from exc


# 注册 DELETE /sessions/{session_id} 接口，用于删除当前用户的一个会话。
@router.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(session_id: str, current_user: User = Depends(get_current_user)):
    """删除当前用户拥有的指定会话及其关联消息。"""
    try:
        # 存储层会检查会话是否存在且归属当前用户；成功删除返回 True。
        deleted = storage.delete_session(current_user.username, session_id)
        if not deleted:
            # 404 Not Found：当前用户没有这个会话，或该会话本身不存在。
            raise HTTPException(status_code=404, detail="会话不存在")
        # 删除成功后返回会话 ID 和提示信息。
        return SessionDeleteResponse(session_id=session_id, message="成功删除会话")
    # 业务主动抛出的 404 要原样保留，不能被通用异常处理误改成 500。
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("删除会话失败")
        raise HTTPException(status_code=500, detail="删除会话失败") from exc
