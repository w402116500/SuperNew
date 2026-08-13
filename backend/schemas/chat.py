"""聊天会话与 RAG 检索接口的 Pydantic 数据模型。"""

# List 表示列表字段；Literal 限制固定选项；Optional 表示字段可以为 None。
from typing import List, Literal, Optional

# BaseModel 提供请求/响应校验和 JSON 序列化；ConfigDict 配置模型行为；
# Field 用于为字段设置默认值、长度范围等额外校验规则。
from pydantic import BaseModel, ConfigDict, Field



# extra=forbid 会拒绝教程尚未定义的字段，及时暴露前后端协议漂移。
class StrictSchema(BaseModel):
    """所有 API Schema 的严格基类，拒绝未声明字段。

    例如客户端发送 ``{"content": "hi", "typo": true}`` 时，未定义的 ``typo``
    不会被静默忽略，而会触发校验错误。
    """
    model_config = ConfigDict(extra="forbid")


class SessionInfo(StrictSchema):
    """会话列表中单个会话的轻量摘要。

    Attributes:
        session_id: 业务层会话标识。
        title: 可选会话标题；未设置时可为 None。
        updated_at: 会话最后更新时间字符串。
        message_count: 当前会话包含的消息数量。
    """
    session_id: str
    title: Optional[str] = None
    updated_at: str
    message_count: int


class SessionListResponse(StrictSchema):
    """会话列表接口的外层响应。"""
    sessions: List[SessionInfo]


class SessionDeleteResponse(StrictSchema):
    """删除会话成功后的确认响应。"""
    session_id: str
    message: str



# 检索片段只暴露引用需要的文件、页码、正文和排名分，不返回 Milvus 内部对象。
class RetrievedChunk(StrictSchema):
    """一个可用于引用和回答的检索片段。

    Attributes:
        filename: 片段来源文件名。
        page_number: PDF 页码或 HTML 章节序号；可能为字符串、整数或 None。
        text: 被检索到的正文内容。
        score: Milvus 召回或融合分数。
        rrf_rank: RRF 融合后的原始排名。
        rerank_score: 可选精排服务给出的相关度分数。
    """
    filename: str
    page_number: Optional[str | int] = None
    text: Optional[str] = None
    score: Optional[float] = None
    rrf_rank: Optional[int] = None
    rerank_score: Optional[float] = None


# RAG trace 采用字段白名单；模型临时输出不能未经验证直接进入 API。
class RagTraceFields(StrictSchema):
    """允许返回给 API 的 RAG 执行诊断字段白名单。

    所有字段默认 ``None``，表示本次流程未产生该信息。由于继承 StrictSchema，任何
    不在此类定义中的模型临时字段都不能直接进入 API 响应。
    """
    # 工具或检索能力是否被调用，以及调用的工具名称。
    tool_used: Optional[bool] = None
    tool_name: Optional[str] = None
    # 原始问题和可选查询改写信息。
    query: Optional[str] = None
    # Literal 表示改写方法只能是 step_back 或 hyde，不能是任意字符串。
    rewrite_method: Optional[Literal["step_back", "hyde"]] = None
    rewritten_query: Optional[str] = None
    step_back_question: Optional[str] = None
    hyde_document: Optional[str] = None
    # 当前检索阶段、路由选择和证据评估结果。
    retrieval_stage: Optional[str] = None
    route: Optional[str] = None
    retrieval_status: Optional[str] = None
    evidence_relevance: Optional[str] = None
    evidence_answerability: Optional[str] = None
    evidence_ambiguity: Optional[str] = None
    evidence_confidence: Optional[float] = None
    evidence_reason: Optional[str] = None
    missing_slots: Optional[List[str]] = None
    # HITL（Human In The Loop，人类介入）暂停、提问和恢复信息。
    hitl_prompt: Optional[str] = None
    hitl_options: Optional[List[str]] = None
    hitl_resumed: Optional[bool] = None
    hitl_answer: Optional[str] = None
    hitl_resume_strategy: Optional[str] = None
    hitl_resume_from_status: Optional[str] = None
    hitl_resume_from_route: Optional[str] = None
    hitl_targeted_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    # 外部 Rerank 服务是否配置、是否实际调用、调用参数和结果统计。
    rerank_enabled: Optional[bool] = None
    rerank_applied: Optional[bool] = None
    rerank_model: Optional[str] = None
    rerank_endpoint: Optional[str] = None
    rerank_error: Optional[str] = None
    rerank_timeout_seconds: Optional[float] = None
    rerank_min_score: Optional[float] = None
    post_rerank_count: Optional[int] = None
    post_threshold_count: Optional[int] = None
    retrieval_empty: Optional[bool] = None
    # Milvus 召回模式和候选池配置/统计。
    retrieval_mode: Optional[str] = None
    retrieval_pipeline: Optional[str] = None
    candidate_k: Optional[int] = None
    candidate_k_source: Optional[str] = None
    candidate_k_config_error: Optional[str] = None
    retrieval_candidate_multiplier: Optional[int] = None
    retrieval_top_k: Optional[int] = None
    recall_count: Optional[int] = None
    post_merge_candidate_count: Optional[int] = None
    candidate_count: Optional[int] = None
    leaf_retrieve_level: Optional[int] = None
    # Auto-merge 父块上卷的开关、阈值、替换数量和执行步数。
    auto_merge_enabled: Optional[bool] = None
    auto_merge_applied: Optional[bool] = None
    auto_merge_threshold: Optional[int] = None
    auto_merge_replaced_chunks: Optional[int] = None
    auto_merge_steps: Optional[int] = None
    retrieved_chunks: Optional[List[RetrievedChunk]] = None
    initial_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    rewrite_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    # 复杂度路由：问题复杂度、拆分出的子问题、子 Agent 数量和最终合并数量。
    complexity: Optional[str] = None
    complexity_reason: Optional[str] = None
    sub_questions: Optional[List[str]] = None
    sub_agent_count: Optional[int] = None
    synthesis_merged_count: Optional[int] = None

class RagSubTrace(RagTraceFields):
    """复杂问题拆分后，单个子问题对应的独立 RAG 诊断信息。

    该类复用 RagTraceFields 的白名单字段，不增加新字段；单独定义它是为了让
    ``RagTrace.sub_traces`` 在类型上明确表示“子问题的 trace 列表”。
    """
    # pass 表示不添加新字段，完全继承父类定义。
    pass


class RagTrace(RagTraceFields):
    """主问题的 RAG 诊断信息，可选包含多个子问题的检索 trace。

    Attributes:
        sub_traces: 复杂问题拆分后，每个子问题对应的 RagSubTrace 列表。
    """
    # Optional 表示简单问题不拆分子问题时，该字段可以不存在或为 None。
    sub_traces: Optional[List[RagSubTrace]] = None


# 跨请求只保存恢复图所需的最小状态，不保存线程、模型或数据库连接。
class HitlResumeState(StrictSchema):
    """跨 HTTP 请求恢复 HITL（人类介入）流程所需的最小可序列化状态。

    不保存线程、模型、数据库连接等无法跨请求恢复的运行时对象，只保存用户问题、当前
    路由、检索状态和有限数量的子问题。

    Attributes:
        question: 原始用户问题，不能为空。
        route: 当前需要用户处理的路由类型。
        retrieval_status: 当前检索暂停状态。
        rewrite_count: 已执行的查询改写次数，不能小于 0。
        complexity: 可选问题复杂度，只能是 simple 或 complex。
        complexity_reason: 判断复杂度的文字原因。
        sub_questions: 复杂问题拆分出的子问题，最多保存 4 条。
    """
    # Field(min_length=1) 确保恢复时至少有一个非空字符的问题。
    question: str = Field(min_length=1)
    # Literal 限制 HITL 当前只支持澄清问题或选择范围两种路由。
    route: Literal["clarify", "scope_select"]
    # Literal 限制检索暂停状态，防止写入未知状态字符串。
    retrieval_status: Literal["needs_clarification", "needs_scope_selection"]
    # 默认未改写过；ge=0 保证计数不会是负数。
    rewrite_count: int = Field(default=0, ge=0)
    complexity: Optional[Literal["simple", "complex"]] = None
    complexity_reason: Optional[str] = None
    # default_factory=list 为每个实例创建独立列表；max_length=4 限制子问题数量。
    sub_questions: List[str] = Field(default_factory=list, max_length=4)

# 规范化时按原列表顺序过滤字段，因为这个顺序就是回答中的 [1]、[2] 引用编号。
def _normalize_chunks(value) -> list[dict]:
    """过滤并校验 trace 中的检索片段，保持原有引用顺序。

    Args:
        value: 可能来自模型或流程临时数据的任意值。

    Returns:
        list[dict]: 仅包含有 filename 的合法 RetrievedChunk 字典；非列表输入返回空列表。
    """
    if not isinstance(value, list):
        # 只有列表才可能是多个检索片段。
        return []
    # model_fields 是 Pydantic 定义的允许字段集合，用它过滤未知字段。
    fields = RetrievedChunk.model_fields
    return [
        # 先按白名单取字段，再由 Pydantic 校验类型，最后去掉值为 None 的字段。
        RetrievedChunk.model_validate({key: item[key] for key in fields if key in item}).model_dump(
            exclude_none=True
        )
        for item in value
        if isinstance(item, dict) and item.get("filename")
    ]


def _normalize_trace_fields(trace: dict, fields: dict) -> dict:
    """按指定 Pydantic 模型字段白名单过滤一个 trace 字典。

    Args:
        trace: 原始 trace 字典，可能带有模型临时字段。
        fields: 目标 Pydantic 模型的 ``model_fields``。

    Returns:
        dict: 过滤未知字段并规范化嵌套 RetrievedChunk 列表后的字典。
    """
    # 字典推导式只保留目标模型定义过的键。
    normalized = {key: trace[key] for key in fields if key in trace}
    # 以下四个字段都包含 RetrievedChunk 列表，因此使用同一函数处理。
    for key in (
        "retrieved_chunks",
        "initial_retrieved_chunks",
        "rewrite_retrieved_chunks",
        "hitl_targeted_retrieved_chunks",
    ):
        if key in normalized:
            normalized[key] = _normalize_chunks(normalized[key])
    return normalized


def normalize_rag_sub_trace(trace: dict | None) -> Optional[dict]:
    """规范化一个子问题 RAG trace，移除未知字段和无效检索片段。

    Args:
        trace: 原始子 trace 字典；可为 None。

    Returns:
        Optional[dict]: 已通过 RagSubTrace 校验的字典；空或非字典输入返回 None。
    """
    if not isinstance(trace, dict) or not trace:
        return None
    normalized = _normalize_trace_fields(trace, RagSubTrace.model_fields)
    # model_validate 进行严格类型校验；model_dump 转回可序列化普通字典。
    return RagSubTrace.model_validate(normalized).model_dump(exclude_none=True)


# 主 trace 和子 trace 都经过 Pydantic 校验，未知字段会在持久化前被剥离。
def normalize_rag_trace(trace: dict | None) -> Optional[dict]:
    """规范化主 RAG trace 及其可选子 trace，供安全持久化或 API 返回。

    Args:
        trace: 原始主 trace 字典；可为 None。

    Returns:
        Optional[dict]: 已通过 RagTrace 校验且移除 None/未知字段的普通字典。
    """
    if not isinstance(trace, dict) or not trace:
        return None
    normalized = _normalize_trace_fields(trace, RagTrace.model_fields)
    if "sub_traces" in normalized:
        # sub_traces 不是列表时按空列表处理，避免逐项遍历字符串或字典。
        sub_traces = normalized["sub_traces"] if isinstance(normalized["sub_traces"], list) else []
        # 仅处理字典子项；无效子项或规范化后为 None 的项会被过滤。
        normalized["sub_traces"] = [
            item
            for item in (
                normalize_rag_sub_trace(sub_trace)
                for sub_trace in sub_traces
                if isinstance(sub_trace, dict)
            )
            if item is not None
        ]
    return RagTrace.model_validate(normalized).model_dump(exclude_none=True)


# 聊天请求体：message 必填；省略 session_id 时才使用默认会话。
class ChatRequest(StrictSchema):
    """客户端发送一条新聊天消息时使用的请求格式。

    ``StrictSchema`` 会拒绝未声明字段。注意 ``session_id`` 的默认值仅在字段缺失时生效；
    客户端显式发送 ``null`` 或空字符串时，Pydantic 会保留该值。
    """

    message: str  # 用户输入的消息正文；该字段必须存在，但当前 Schema 允许空字符串。
    session_id: Optional[str] = "default_session"  # 会话标识；省略时归入默认会话。
    # 可选来源范围只能收窄本次检索候选；省略时维持全知识库检索的既有行为。
    knowledge_filenames: Optional[List[str]] = None

# pending HITL 保存原问题、提示、历史补充和恢复状态，供下一次 HTTP 请求继续。
class PendingHitlState(StrictSchema):
    """一次暂停的 HITL（人工介入）对话状态。

    RAG 发现问题缺少条件或存在多个候选范围时，服务保存此对象而不是保存整个图运行时。
    用户下一次补充答案后，可用 ``resume_state`` 从暂停位置继续定向检索。
    """

    id: str = Field(min_length=1)  # 此次 HITL 暂停记录的唯一 ID，不能为空。
    original_question: str = Field(min_length=1)  # 触发暂停的原始用户问题。
    prompt: str = Field(min_length=1)  # 服务需要展示给用户的澄清或选择提示。
    options: List[str] = Field(default_factory=list)  # scope_select 时可展示的候选项；默认空列表。
    route: Literal["clarify", "scope_select"]  # 暂停原因：补充条件或从多个范围中选择。
    retrieval_status: Literal["needs_clarification", "needs_scope_selection"]  # 与 route 对应的 API 状态。
    answers: List[str] = Field(default_factory=list)  # 用户历次补充的答案，保留顺序。
    resume_state: HitlResumeState  # 恢复 RAG 所需的最小、已校验状态快照。
    created_at: str  # 创建暂停记录的时间字符串，通常为 ISO 8601 格式。

# 非流式响应同时返回正文和经过规范化的 rag_trace。
class ChatResponse(StrictSchema):
    """一次非流式聊天接口调用成功后的响应格式。"""

    response: str  # 回答模型生成并返回给客户端的正文。
    rag_trace: Optional[RagTrace] = None  # 可选 RAG 诊断信息；没有调用检索时为 None。


class MessageInfo(StrictSchema):
    """会话历史中一条已持久化消息的格式。"""

    type: str  # 消息角色，例如 human、ai 或 system。
    content: str  # 消息文本内容；用户问题和 AI 回答都存放在此字段。
    timestamp: str  # 消息创建或保存时间的字符串。
    rag_trace: Optional[RagTrace] = None  # AI 消息可附带检索轨迹；普通用户消息通常为 None。


class SessionMessagesResponse(StrictSchema):
    """读取一个会话全部消息时的外层响应。"""

    messages: List[MessageInfo]  # 按对话顺序排列的聊天历史消息。
