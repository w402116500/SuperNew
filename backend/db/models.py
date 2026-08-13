# UTC 是世界协调时间；datetime 用于模型中的创建、更新时间字段。
from datetime import UTC, datetime

# 新模型会用到外键、JSON 正文和复合唯一约束，先把这些 SQLAlchemy 类型一次导入。
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
# Mapped 用于标注 ORM 字段类型；mapped_column 定义数据库列；relationship 定义模型间关系。
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Base 是所有 ORM 模型的父类。继承它的类会被 SQLAlchemy 映射为数据库表。
from backend.infra.database import Base


def _utc_now_naive() -> datetime:
    """返回当前 UTC 时间，并移除时区信息以匹配数据库的 DateTime 字段。

    Returns:
        datetime: 不带 ``tzinfo`` 的当前 UTC 时间。
    """
    # datetime.now(UTC) 先获取带 UTC 时区的当前时间；replace(...) 去掉 tzinfo。
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    """用户表模型：保存账号、密码哈希、角色和创建时间。

    创建示例：
        ``User(username="alice", password_hash="...", role="user")``

    Attributes:
        id: 数据库自动生成的用户主键。
        username: 用户登录名；不能为空且全表唯一。
        password_hash: 密码经过 PBKDF2 等算法处理后的不可逆哈希，不保存明文密码。
        role: 用户角色，例如 ``"user"`` 或 ``"admin"``；默认是 ``"user"``。
        created_at: 用户记录创建时的 UTC 时间。
        sessions: 该用户拥有的 ``ChatSession`` 会话对象列表（ORM 关系，不是数据库列）。
    """

    # 指定该模型对应数据库中的 users 表。
    __tablename__ = "users"

    # Mapped[int] 表示 Python 中该属性是 int；mapped_column(...) 定义对应的数据库列。
    # primary_key=True：每条记录的唯一标识，通常由数据库自动生成。
    # index=True：为常用查询字段创建索引，加快按 id 查找的速度。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # String(100) 最多保存 100 个字符；unique=True 禁止重复用户名；nullable=False 表示必填。
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    # 数据库存的是不可逆的密码哈希，而不是用户输入的明文密码。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 用户角色。创建记录但未指定时，数据库默认保存 "user"。
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    # default 接收函数本身，不加括号；每次插入新用户时才调用 _utc_now_naive()。
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)

    # relationship 不是 users 表的一列，而是 SQLAlchemy 提供的对象关系属性。
    # 可通过 user.sessions 获取该用户的全部 ChatSession 对象。
    # 删除用户时级联删除其会话，避免留下失去所有者的数据。
    sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")


class ChatSession(Base):
    """聊天会话表模型：一个用户可以拥有多个会话。

    创建示例：
        ``ChatSession(user_id=1, session_id="default_session", metadata_json={})``

    Attributes:
        id: 数据库自动生成的会话主键。
        user_id: 所属用户的主键，引用 ``users.id``。
        session_id: 业务层使用的会话标识；只要求在同一用户内唯一。
        metadata_json: 会话附加信息字典，例如 ``{"title": "Python 学习"}``。
        updated_at: 会话最近一次保存或更新的 UTC 时间。
        created_at: 会话首次创建的 UTC 时间。
        user: 该会话所属的 ``User`` 对象（ORM 关系，不是数据库列）。
        messages: 会话中的 ``ChatMessage`` 消息对象列表（ORM 关系，不是数据库列）。
    """

    # 此模型对应数据库中的 chat_sessions 表。
    __tablename__ = "chat_sessions"
    # session_id 只要求在同一用户内唯一，不同用户可以各自使用 default_session。
    # UniqueConstraint 是复合唯一约束：user_id 与 session_id 的组合不能重复。
    __table_args__ = (UniqueConstraint("user_id", "session_id", name="uq_user_session"),)

    # 会话记录的主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # ForeignKey("users.id") 表示该字段引用 users 表的 id 字段。
    # ondelete="CASCADE" 表示从数据库层删除用户时，关联会话也会被删除。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # 业务定义的会话名称或标识，例如 default_session；它不是数据库主键。
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # JSON 列可保存 Python 字典序列化后的结构化数据，例如会话标题或其他扩展信息。
    # default=dict 每次创建会话时生成新的空字典，避免多个对象共享同一个字典。
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 最近一次更新会话内容的时间。
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    # 会话首次创建的时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)

    # 与 User.sessions 建立双向关系：session.user 可以取得所属用户。
    user = relationship("User", back_populates="sessions")
    # messages 同样不是数据库列，可通过 session.messages 取得会话的全部消息。
    # 消息生命周期从属于会话，删除会话时由数据库关系一并清理。
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    """聊天消息表模型：一条记录表示会话中的一条用户、AI 或系统消息。

    创建示例：
        ``ChatMessage(session_ref_id=10, message_type="human", content="你好")``

    Attributes:
        id: 数据库自动生成的消息主键。
        session_ref_id: 所属聊天会话的主键，引用 ``chat_sessions.id``。
        message_type: 消息角色，例如 ``"human"``、``"ai"`` 或 ``"system"``。
        content: 消息正文文本。
        timestamp: 消息产生或保存时的 UTC 时间。
        session: 该消息所属的 ``ChatSession`` 对象（ORM 关系，不是数据库列）。
    """

    # 此模型对应数据库中的 chat_messages 表。
    __tablename__ = "chat_messages"

    # 消息记录的主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 每条消息通过数据库外键归属一个会话，不能只在 Python 对象中维持关系。
    # 删除会话时，ondelete="CASCADE" 会让数据库同时删除其消息。
    session_ref_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    # 消息角色，例如 human（用户）、ai（模型）或 system（系统提示）。
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Text 适合保存长度不固定的长文本消息。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 该消息产生或保存的时间。
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    # 消息 trace 到聊天服务章才获得数据库列；普通消息仍可保持 NULL。
    rag_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 与 ChatSession.messages 建立双向关系：message.session 可以取得所属会话。
    session = relationship("ChatSession", back_populates="messages")



class ParentChunk(Base):
    """文档切分后的父块表模型，用于 Auto-merging 检索恢复上下文。

    PostgreSQL 只保存 L1、L2 等父块；更细粒度的 L3 叶子块会写入 Milvus 向量库。

    创建示例：
        ``ParentChunk(chunk_id="doc1-l1-0", text="...", filename="guide.pdf")``

    Attributes:
        chunk_id: 块的稳定唯一标识，也是主键；重复导入时可据此更新同一块。
        text: 当前文档块的完整文本内容。
        filename: 原始文件名，例如 ``"guide.pdf"``；已建立索引以便按文件查询。
        file_type: 文件类型，例如 ``"pdf"``、``"docx"``；默认空字符串。
        file_path: 原文件在存储系统中的路径；默认空字符串。
        page_number: 当前块在原文件中的页码；无页码时为 ``0``。
        parent_chunk_id: 直接父块的 ID；顶层块可为空字符串。
        root_chunk_id: 所属最顶层 L1 块的 ID；顶层块可为空字符串。
        chunk_level: 块所在层级，例如 L1 为 ``1``、L2 为 ``2``；默认 ``0``。
        chunk_idx: 同一层级中的顺序编号；默认 ``0``。
        updated_at: 当前父块最后写入或更新时的 UTC 时间。
    """

    # PostgreSQL 只保存可供 Auto-merging 恢复的 L1/L2 父块，L3 叶子块稍后写入 Milvus。
    __tablename__ = "parent_chunks"

    # 稳定且唯一的 chunk_id 让重复上传可以更新同一父块，而不是不断插入副本。
    chunk_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    # 当前块的正文；Text 可保存长度不固定的文档内容。
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 原始文件名。index=True 加快按文件查找所有块的操作。
    filename: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # 文件扩展或类别，例如 pdf、docx；未提供时使用空字符串。
    file_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    # 原始文件路径；未提供时使用空字符串。
    file_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    # 原文件页码。普通文本等无分页来源可保持默认值 0。
    page_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # parent_chunk_id 指向直接父块，root_chunk_id 则始终指向最上层 L1。
    parent_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    root_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # 文档分块层级和该层级中的顺序，用于重建父子结构和原文顺序。
    chunk_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 每次插入新块时自动设置当前 UTC 时间。
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
