from backend.schemas.auth import (
    AuthResponse,
    CurrentUserResponse,
    LoginRequest,
    RegisterRequest,
)
from backend.schemas.chat import (
    # 这里只导出会话读取所需类型，聊天请求和 RAG 类型仍不属于当前阶段。
    MessageInfo,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse, HitlResumeState, RagSubTrace, RagTrace, RetrievedChunk,
    ChatRequest,PendingHitlState,ChatResponse
)

from backend.schemas.documents import (
    # 文档协议到本章才进入公共 Schema 出口，RAG 和聊天类型仍不提前导出。
    DocumentDeleteJobResponse,
    DocumentDeleteResponse,
    DocumentDeleteStartResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadJobResponse,
    DocumentUploadResponse,
    DocumentUploadStartResponse,
    DocumentBatchUploadStartResponse,
    UploadStepInfo,
)

__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "AuthResponse",
    "CurrentUserResponse",
    "MessageInfo",
    "SessionMessagesResponse",
    "SessionInfo",
    "SessionListResponse",
    "SessionDeleteResponse",
    "DocumentInfo",
    "DocumentListResponse",
    "DocumentUploadResponse",
    "DocumentUploadStartResponse",
    "DocumentBatchUploadStartResponse",
    "UploadStepInfo",
    "DocumentUploadJobResponse",
    "DocumentDeleteStartResponse",
    "DocumentDeleteJobResponse",
    "RetrievedChunk",
    "RagTrace",
    "RagSubTrace",
    "HitlResumeState",
    "ChatRequest",
    "PendingHitlState",
    "ChatResponse",

]
