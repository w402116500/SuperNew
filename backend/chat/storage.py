"""聊天会话的持久化与缓存访问层。

PostgreSQL 是消息和会话元数据的事实来源；Redis 只缓存消息列表和会话列表。每次写入
先提交数据库，再更新或失效缓存，避免缓存记录了数据库中不存在的对话内容。
"""

# UTC 时间用于统一保存消息和会话更新时间，datetime 用于创建当前时间戳。
from datetime import UTC, datetime

# 将存储中的字典记录转换回 LangChain 对话对象。
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# ChatMessage/ChatSession/User 是数据库模型；cache 与 SessionLocal 分别访问 Redis 和 PostgreSQL。
from backend.db.models import ChatMessage, ChatSession, User
from backend.infra.cache import cache
from backend.infra.database import SessionLocal
from backend.schemas.chat import normalize_rag_trace


class ConversationStorage:
    """管理聊天消息、会话元数据及其 Redis 缓存。

    ``user_id`` 在接口层是用户名；本类先查询对应 ``User``，再使用数据库内部 ``user.id``
    读取会话，确保不同用户可以安全使用相同的 ``session_id``。
    """

    @staticmethod
    def _messages_cache_key(user_id: str, session_id: str) -> str:
        """生成一个用户某会话的消息缓存键。"""
        return f"chat_messages:{user_id}:{session_id}"  # 同时加入用户和会话，避免不同用户缓存冲突。

    @staticmethod
    def _sessions_cache_key(user_id: str) -> str:
        """生成一个用户的会话列表缓存键。"""
        return f"chat_sessions:{user_id}"  # 会话列表按用户隔离，不与具体 session_id 绑定。

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        """将数据库/Redis 中的消息字典转换为 LangChain 消息对象。"""
        messages = []  # 存放按原顺序重建的 LangChain 消息。
        for msg_data in records:  # 每一项通常含 type、content、timestamp 和可选 rag_trace。
            msg_type = msg_data.get("type")  # 读取消息角色。
            content = msg_data.get("content", "")  # 缺少正文时用空字符串，避免构造消息失败。
            if msg_type == "human":
                messages.append(HumanMessage(content=content))  # 用户消息对应 HumanMessage。
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))  # 模型回答对应 AIMessage。
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))  # 系统提示对应 SystemMessage。
        return messages  # 未识别的 type 被忽略，不会污染 Agent 上下文。

    # 无论记录来自 Redis 还是 PostgreSQL，返回前都按同一 Schema 规范化 trace。
    @staticmethod
    def _normalize_message_records(records: list[dict]) -> list[dict]:
        """复制消息记录并使用当前 Schema 规范化其中的 RAG trace。"""
        normalized = []  # 保存规范化后的新列表，不直接修改 Redis 读取的原对象。
        for record in records:
            current = dict(record)  # 浅复制保留 type/content/timestamp 等原字段。
            current["rag_trace"] = normalize_rag_trace(record.get("rag_trace"))  # 删除旧 trace 的未知字段。
            normalized.append(current)
        return normalized

    def save(
        self,
        user_id: str,
        session_id: str,
        messages: list,
        metadata: dict = None,
        # 额外数据按消息索引对齐，只有对应 AI 消息携带本轮 rag_trace。
        extra_message_data: list = None,
    ):
        """将一整个会话的消息快照写入数据库，并刷新相关 Redis 缓存。

        当前实现先删除该会话旧消息，再按 ``messages`` 参数重建全部消息。因此调用方应传入
        完整会话历史，而不是只传增量的一条消息。
        """
        db = SessionLocal()  # 为本次写入创建独立 SQLAlchemy 数据库会话。
        try:
            user = db.query(User).filter(User.username == user_id).first()  # 将外部用户名映射为内部用户记录。
            if not user:  # 不存在的用户不创建孤立会话或消息。
                return

            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:  # 首次保存此 session_id 时创建会话主记录。
                session = ChatSession(user_id=user.id, session_id=session_id, metadata_json=metadata or {})  # metadata 缺失时存空字典。
                db.add(session)  # 将新会话加入待提交事务。
                db.flush()  # 立即取得 session.id，供下方 ChatMessage 外键使用。
            elif metadata is not None:  # 已存在会话且调用方提供新元数据时才更新。
                existing_meta = session.metadata_json or {}  # 兼容旧会话 metadata 为 None 的情况。
                session.metadata_json = {**existing_meta, **metadata}  # 新字段覆盖同名旧字段，其余字段保留。

            db.query(ChatMessage).filter(ChatMessage.session_ref_id == session.id).delete(synchronize_session=False)  # 删除旧快照，准备重建完整消息列表。

            serialized = []  # 同时构造可直接写入 Redis 的纯 JSON 消息字典。
            now = datetime.now(UTC).replace(tzinfo=None)  # 使用统一 UTC 时间；数据库列存无时区时间。
            for idx, msg in enumerate(messages):  # idx 与 extra_message_data 中对应消息位置对齐。
                rag_trace = None  # 默认用户消息或无附加数据的消息不保存检索轨迹。
                if extra_message_data and idx < len(extra_message_data):  # 防止附加列表比消息列表短时越界。
                    extra = extra_message_data[idx] or {}  # None 项也按空字典处理。
                    # 写数据库前剥离未知模型字段，历史接口不会原样回放不受控字典。
                    rag_trace = normalize_rag_trace(extra.get("rag_trace"))

                db.add(  # 将每条 LangChain 消息转成一行 ChatMessage 并加入事务。
                    ChatMessage(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        content=str(msg.content),
                        timestamp=now,
                        rag_trace=rag_trace,
                    )
                )
                serialized.append(  # Redis 使用同样的字段结构缓存，无需再次查询数据库。
                    {
                        "type": msg.type,
                        "content": str(msg.content),
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
                    }
                )

            session.updated_at = now  # 会话列表按该时间倒序排列。
            db.commit()  # 一次提交会话、删除旧消息、新消息、metadata 和 rag_trace。

            # 前一行 commit 已把消息、metadata 和 trace 一起提交；任一写入失败都不会执行这里。
            # commit 成功后才刷新消息缓存，页面刷新与数据库读取保持一致。
            cache.set_json(self._messages_cache_key(user_id, session_id), serialized)
            cache.delete(self._sessions_cache_key(user_id))
        finally:
            db.close()  # 无论提前 return、提交失败或成功都归还数据库连接。

    def load(self, user_id: str, session_id: str) -> list:
        """读取会话并转换为供 LangChain/Agent 使用的消息对象列表。"""
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))  # 先尝试 Redis，降低数据库读取频率。
        if cached is not None:  # 空列表也属于合法缓存命中，因此必须用 is not None 判断。
            return self._to_langchain_messages(cached)  # 缓存字典转换为 HumanMessage/AIMessage 等对象。

        records = self.get_session_messages(user_id, session_id)  # 缓存未命中时从数据库读取标准字典记录。
        cache.set_json(self._messages_cache_key(user_id, session_id), records)  # 回填缓存，供下一次 load 直接命中。
        return self._to_langchain_messages(records)  # 数据库字典同样转换为 LangChain 对象。

    def load_with_meta(self, user_id: str, session_id: str) -> tuple[list, dict]:
        """加载对话消息及会话元数据（标题、持久化笔记等）。"""
        messages = self.load(user_id, session_id)  # 消息优先走缓存或缓存回退路径。
        db = SessionLocal()  # metadata 不缓存，因此额外打开数据库会话读取。
        try:
            user = db.query(User).filter(User.username == user_id).first()  # 验证用户并获取内部主键。
            if not user:
                return messages, {}
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:  # 会话不存在时仍返回已读取的消息及空 metadata。
                return messages, {}
            # 消息可以走缓存，但会话 metadata 仍从 PostgreSQL 读取，避免缓存模型混入额外职责。
            return messages, dict(session.metadata_json or {})  # 复制 metadata，避免调用方修改 ORM 原字典。
        finally:
            db.close()

    def list_session_infos(self, user_id: str) -> list[dict]:
        """列出用户全部会话摘要，按最近更新时间倒序排列。"""
        cached = cache.get_json(self._sessions_cache_key(user_id))  # 会话列表有独立缓存，不与消息缓存混用。
        if cached is not None:
            return cached

        db = SessionLocal()  # 列表缓存未命中时才查询 PostgreSQL。
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []

            sessions = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .all()
            )
            result = []  # 构造 API 层需要的轻量会话摘要。
            for s in sessions:
                count = db.query(ChatMessage).filter(ChatMessage.session_ref_id == s.id).count()  # 统计当前会话的历史消息数。
                # 会话标题来自服务端 metadata；没有标题时才回退到 session_id。
                meta = s.metadata_json or {}
                result.append(
                    {
                        "session_id": s.session_id,  # 客户端继续聊天或删除会话所需的业务 ID。
                        "title": meta.get("title") or s.session_id,  # 未设置标题时用 session_id 兜底显示。
                        "updated_at": s.updated_at.isoformat(),  # 转成 JSON 可序列化的时间字符串。
                        "message_count": count,  # 当前会话包含的消息总数。
                    }
                )
            cache.set_json(self._sessions_cache_key(user_id), result)
            return result
        finally:
            db.close()

    def get_session_messages(self, user_id: str, session_id: str) -> list[dict]:
        """读取 JSON 形式的消息记录；这是 API 响应和缓存使用的底层读取方法。"""
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))  # 先读取会话消息缓存。
        if cached is not None:
            # 缓存命中也要重新规范化，旧缓存缺字段或含未知字段时会被修正。
            normalized = self._normalize_message_records(cached)  # 用当前 Schema 修正旧缓存 trace。
            if normalized != cached:  # 只有内容发生变化时才写回，避免无意义 Redis 写入。
                cache.set_json(self._messages_cache_key(user_id, session_id), normalized)
            return normalized

        db = SessionLocal()  # Redis 未命中后从 PostgreSQL 读取。
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return []

            rows = (  # 按数据库自增 ID 升序恢复真实对话顺序。
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            result = [  # 将 ORM 行转换为缓存/API 可序列化的普通字典。
                {
                    "type": row.message_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    # 数据库中的旧 trace 也在读取时重新规范化，兼容字段演进。
                    "rag_trace": normalize_rag_trace(row.rag_trace),
                }
                for row in rows
            ]
            cache.set_json(self._messages_cache_key(user_id, session_id), result)  # 数据库读取成功后回填 Redis。
            return result
        finally:
            db.close()


    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除一个用户会话及其消息，并在提交成功后清理相关缓存。"""
        db = SessionLocal()  # 为删除事务创建独立数据库会话。
        try:
            user = db.query(User).filter(User.username == user_id).first()  # 先验证用户归属。
            if not user:
                return False
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:  # 不存在的会话无需删除，返回 False 给调用方。
                return False

            db.delete(session)  # 删除会话主记录；关联消息由数据库级级联策略处理。
            db.commit()  # 先提交数据库事实，成功后才执行缓存失效。
            # 删除会话同样先提交数据库事实，再清理消息和列表两类缓存。
            cache.delete(self._messages_cache_key(user_id, session_id))
            cache.delete(self._sessions_cache_key(user_id))
            return True
        finally:
            db.close()


storage = ConversationStorage()  # 模块级共享实例，路由和服务可直接导入使用。
