"""带证据评分、查询改写、复杂度分流和 HITL 恢复能力的 RAG 编排图。

简单问题沿着“检索 -> 证据评分 -> 可选一次改写”的主路径执行；复杂问题会被拆分为
多个独立子问题并行检索，最后再合成去重后的证据。检索证据不足且需要用户补充时，
图会返回最小的可持久化 HITL 状态，下一次请求再从该状态定向恢复。
"""

# Annotated 为 LangGraph reducer 声明字段合并规则；TypedDict 描述图中节点共享状态。
from typing import Annotated, Any, List, Literal, Optional, TypedDict
# operator.add 是 sub_results 的 reducer，用于合并并行子 Agent 的返回结果。
import operator
# os 读取模型地址、密钥和模型名；re 为简单问题快速判断提供正则处理。
import os
import re
# LangChain 创建 OpenAI 兼容模型；LangGraph 负责节点、边和并行 Send 扇出。
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, END
from langgraph.types import Send
# Pydantic 约束模型输出的证据评分和复杂度计划，避免用自由文本驱动路由。
from pydantic import BaseModel, Field

# Context 提供本请求的 SSE 步骤事件和 trace 暂存，不使用跨请求全局状态。
from backend.chat.request_context import ChatRequestContext
# 规划和评分也使用同一硬超时，避免外部服务阻塞编排图。
from backend.model_settings import model_timeout_seconds
# 恢复状态和子 Agent trace 都先经过 Schema 校验与白名单规范化。
from backend.schemas.chat import HitlResumeState, normalize_rag_sub_trace
# utils 提供底层检索、一次改写、去重和 API trace 字段过滤能力。
from backend.rag.utils import (
    RETRIEVAL_TOP_K,
    RetrievalRuntime,
    retrieve_documents,
    fuse_rewrite_candidate_results,
    rewrite_query_once,
    dedupe_documents,
    retrieval_trace_fields,
    invoke_structured_output,
)

# 模型凭据和地址只从环境读取，不能硬编码进编排图。
API_KEY = os.getenv("ARK_API_KEY")  # 调用模型服务时使用的认证密钥。
BASE_URL = os.getenv("BASE_URL")  # OpenAI 兼容模型服务的基础地址。
FAST_MODEL = os.getenv("FAST_MODEL")  # 用于快速分类复杂度和拆分子问题的模型名。
GRADE_MODEL = os.getenv("GRADE_MODEL")  # 用于判断检索证据是否充分的模型名。
MODEL_TIMEOUT_SECONDS = model_timeout_seconds()  # 单次规划、评分模型请求的硬超时。

_grader_model = None  # 证据评分模型客户端缓存；首次创建后复用。
_complexity_model = None  # 复杂度规划模型客户端缓存；首次创建后复用。


# 证据评分模型单独初始化；未配置时返回 None，由评分节点报告明确配置错误。
def _get_grader_model():
    """按需创建并缓存证据评分模型；缺少配置时返回 None。

    评分模型与快速规划模型分开，避免复杂度分类占用更昂贵或更严格的证据判断模型。
    """
    # 允许函数把首次创建的客户端写入模块级缓存。
    global _grader_model
    # 没有认证密钥或评分模型名时，无法调用证据评分服务。
    if not API_KEY or not GRADE_MODEL:
        # None 表示配置缺失，调用方据此抛出明确的配置错误。
        return None
    # 只在首次需要评分时创建客户端，后续请求复用它。
    if _grader_model is None:
        # 创建 OpenAI 兼容的聊天模型对象；此处尚未发送评分请求。
        _grader_model = init_chat_model(
            # 使用独立 GRADE_MODEL 执行证据充分性判断。
            model=GRADE_MODEL,
            # 按 OpenAI 兼容协议构造请求。
            model_provider="openai",
            # 从环境变量取得认证密钥。
            api_key=API_KEY,
            # 使用环境中配置的 API 根地址。
            base_url=BASE_URL,
            # 评分是流程决策，因此固定温度以获得稳定结果。
            temperature=0,
            # 收集流式用量统计，便于统一模型调用指标。
            stream_usage=True,
            # 超时后证据评分节点会进入既有的 fail-closed 分支。
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    # 返回缓存的评分模型供评分节点调用。
    return _grader_model


def _get_complexity_model():
    """按需创建复杂度规划模型；FAST_MODEL 用于分类和子问题分解。"""
    # 声明后，函数内对 _complexity_model 的赋值会修改模块级缓存，而不是新建局部变量。
    global _complexity_model
    # API 密钥和快速模型名任何一个缺失时都无法创建客户端，交给调用节点决定如何处理。
    if not API_KEY or not FAST_MODEL:
        # 返回 None 表示“未配置”，而不是模型调用失败或模型给出了空回答。
        return None
    # 第一次调用时缓存还是 None；后续调用直接复用已创建的客户端，避免重复初始化。
    if _complexity_model is None:
        # init_chat_model 返回 LangChain 聊天模型客户端，此时仅创建客户端，不会发送请求。
        _complexity_model = init_chat_model(
            # 使用 .env 中的 FAST_MODEL，复杂度分类和拆题通常使用低延迟模型即可。
            model=FAST_MODEL,
            # 告诉 LangChain 按 OpenAI 兼容 API 的协议组织请求。
            model_provider="openai",
            # 调用模型服务的认证密钥，仅从环境变量读取，不能写入源码或日志。
            api_key=API_KEY,
            # OpenAI 兼容服务的根地址；可指向官方地址或自建/第三方兼容网关。
            base_url=BASE_URL,
            # 温度为 0，使同一问题的分类和拆题结果尽可能稳定、可复现。
            temperature=0,
            # 让客户端收集流式调用的用量信息；本函数本身并不会开启文本流式输出。
            stream_usage=True,
            # 规划请求超时后由调用方显式处理，而不是无限等待。
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    # 返回新建或此前缓存的同一个模型客户端，供 classify_complexity() 调用。
    return _complexity_model


EVIDENCE_GRADE_PROMPT = (
    "你是 RAG 证据评分器。请只根据检索片段判断它们是否足以回答用户问题，"
    "不要补充片段里没有的信息。\n\n"
    "用户问题：\n{question}\n\n"
    "检索片段：\n{context}\n\n"
    "请按以下规则给出结构化结果：\n"
    "- relevance: none 表示主题不相关；weak 表示主题接近但证据弱；strong 表示主题明确相关。\n"
    "- answerability: none 表示不能回答；partial 表示有部分线索但不足以给确定答案；"
    "sufficient 表示片段能直接或组合支撑答案。\n"
    "- 对“是否使用/是否存在/是不是”等是非事实问题，只要片段直接提及该事实，就应判为 sufficient；"
    "不要索要用户没有询问的实现细节。\n"
    "- ambiguity: missing_slot 表示缺少角色名、版本、文件类型、模块名、产品线等关键条件；"
    "multiple_candidates 表示多个候选方向都可能相关；none 表示无明显歧义。\n"
    "- route 只能选择：answer、rewrite、clarify、scope_select、no_knowledge。\n"
    "  answer: relevance=strong 且 answerability=sufficient。\n"
    "  rewrite: 有相关信号，但像是问法、别名或泛化程度导致证据不足。\n"
    "  clarify: 缺少关键条件，需要用户补充。\n"
    "  scope_select: 多个候选方向都相关，需要用户选择。\n"
    "  no_knowledge: 无召回或主题不相关。\n"
    "- 如果 route 是 clarify 或 scope_select，请给 hitl_prompt；如果能列出选项，请给 hitl_options。"
)


class EvidenceGrade(BaseModel):
    """结构化证据评分：同时判断相关性、可回答性与下一步路由。"""

    relevance: Literal["none", "weak", "strong"] = Field(  # 检索片段与问题的主题相关性。
        description="检索片段与问题的主题相关性"
    )
    answerability: Literal["none", "partial", "sufficient"] = Field(  # 证据能否支撑回答。
        description="检索片段是否足以回答问题"
    )
    ambiguity: Literal["none", "missing_slot", "multiple_candidates"] = Field(  # 是否缺少条件或存在多个候选方向。
        default="none",
        description="问题是否缺条件或存在多个候选方向"
    )
    route: Literal["answer", "rewrite", "clarify", "scope_select", "no_knowledge"] = Field(  # 模型建议的下一跳，代码仍会校正。
        description="下一步路由"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # 评分模型给出的 0 到 1 置信度。
    missing_slots: List[str] = Field(default_factory=list)  # 用户还需补充的条件，例如版本或模块。
    hitl_prompt: str = ""  # 进入澄清时展示给用户的提问文案。
    hitl_options: List[str] = Field(default_factory=list)  # 进入范围选择时展示的候选选项。
    reason: str = ""  # 评分或路由原因，便于排查未直接回答的原因。


class ComplexityResult(BaseModel):
    """问题复杂度分类结果。"""

    complexity: Literal["simple", "complex"] = Field(  # simple 走标准检索，complex 拆成并行子问题。
        description="问题复杂度：'simple' 为简单问题，'complex' 为复杂问题"
    )
    reason: str = Field(default="", description="分类理由")  # 模型给出的复杂度分类依据。
    sub_questions: List[str] = Field(  # complex 时生成的独立检索子问题，最多四项。
        default_factory=list,
        description="复杂问题对应的 2-4 个可独立检索子问题；简单问题留空",
        max_length=4,
    )


# 所有节点只通过 RAGState 交换问题、文档、路由和 trace，避免隐藏的全局状态。
class RAGState(TypedDict):
    """LangGraph 节点间传递的完整状态契约。

    节点应只返回自己更新的字段，LangGraph 会将更新合并进当前状态；其中
    ``sub_results`` 使用 ``operator.add`` 合并并行子 Agent 的列表结果。
    """

    # 用户原问题保持不变；query 可在重写或 HITL 恢复后变成实际检索查询。
    question: str  # 用户最初提出的问题，改写和恢复过程中保持不变。
    query: str  # 当前实际发给检索器的查询文本。
    context: str  # 按引用编号格式化后的证据正文，供评分或回答模型使用。
    docs: List[dict]  # 当前检索到的原始文档块字典列表。
    # route 控制下一跳，retrieval_status 用于对外表达当前证据状态。
    route: Optional[str]  # 当前节点决定的下一步动作，例如 answer、rewrite 或 clarify。
    retrieval_status: Optional[str]  # answerable、partial 等供 API 使用的稳定状态。
    evidence_relevance: Optional[str]  # 评分模型输出的主题相关性。
    evidence_answerability: Optional[str]  # 评分模型输出的可回答性。
    evidence_ambiguity: Optional[str]  # 评分模型输出的歧义类别。
    evidence_confidence: Optional[float]  # 评分模型给出的 0 到 1 置信度。
    missing_slots: Optional[List[str]]  # 需要用户补充的关键条件列表。
    hitl_prompt: Optional[str]  # 澄清/范围选择时展示的提问文案。
    hitl_options: Optional[List[str]]  # 可供用户选择的候选范围。
    # rewrite_count 是图的硬预算，最多允许一次查询重写。
    rewrite_count: int  # 已执行的查询改写次数；主流程硬限制为最多一次。
    rewrite_method: Optional[str]  # 本轮选择的 Step-back 或 HyDE 方法。
    rewritten_query: Optional[str]  # 拼接改写信息后的第二次检索查询。
    step_back_question: Optional[str]  # Step-back 产生的更抽象问题。
    hyde_document: Optional[str]  # HyDE 产生的假设文档，仅用于检索。
    initial_retrieval: Optional[dict]
    rewrite_failed: bool
    candidate_audits: Optional[List[dict]]
    # rag_trace 最终会被 Schema 过滤后随 AI 消息持久化。
    rag_trace: Optional[dict]  # 本轮检索的结构化诊断记录，最终会随消息保存。
    # 复杂度路由新增字段
    complexity: Optional[str]  # simple 或 complex 分类结论。
    complexity_reason: Optional[str]  # 分类结论的解释文本。
    sub_questions: Optional[List[str]]  # 复杂问题拆出的独立检索问题。
    # 子 Agent 禁用二次重写，防止复杂问题的并行分支无限扩张。
    is_sub_agent: bool  # True 表示当前是复杂问题的子分支，因此禁用二次改写。
    sub_results: Annotated[List[dict], operator.add]  # 并行分支结果按列表追加而不是覆盖。
    request_context: ChatRequestContext  # 当前请求的 SSE 队列、trace 和预算上下文。
    knowledge_filenames: List[str]  # 本请求可检索的文件名白名单；空列表表示不限制。
    rag_step_group: Optional[str]  # 前端显示并行步骤时使用的分组 ID。
    rag_step_group_label: Optional[str]  # 前端显示给用户的分组标题。
    retrieval_runtime: Optional[RetrievalRuntime]  # 评测可注入独立集合；线上默认为 None。


def _format_docs(docs: List[dict]) -> str:
    """将检索块格式化为带来源编号的提示词上下文。

    编号与输入列表顺序一致，回答模型可以用 ``[1]``、``[2]`` 引用来源；因此合成阶段
    的去重和排序必须在调用本函数之前完成。
    """
    # 没有任何证据块时不生成空白分隔符。
    if not docs:
        return ""
    # 收集格式化后的每个来源块，最后用分隔线拼接。
    chunks = []
    # enumerate 从 1 开始，使引用编号更符合用户阅读习惯。
    for i, doc in enumerate(docs, 1):
        source = doc.get("filename", "Unknown")  # 来源文件缺失时显示 Unknown。
        page = doc.get("page_number", "N/A")  # 页码或章节缺失时显示 N/A。
        text = doc.get("text", "")  # 正文缺失时使用空字符串，避免格式化报错。
        chunks.append(f"[{i}] {source} (Page {page}):\n{text}")  # 写入编号和正文。
    return "\n\n---\n\n".join(chunks)


def _copy_jsonable_doc(doc: dict) -> dict:
    """复制恢复快照所需的白名单字段，避免保存 Milvus 或模型的内部对象。"""
    # 仅允许这些纯数据字段进入跨请求恢复快照。
    allowed = {
        "filename",
        "page_number",
        "text",
        "score",
        "rrf_rank",
        "rerank_score",
        "chunk_id",
        "doc_id",
    }
    # 字典推导式过滤掉向量、客户端句柄等不应序列化的额外字段。
    return {key: value for key, value in doc.items() if key in allowed}


def _copy_jsonable_docs(docs: List[dict] | None) -> List[dict]:
    """批量复制可 JSON 序列化的检索块；None 或非字典项会被忽略。"""
    # 先把 None 变为空列表，再跳过非字典项并逐个复制白名单字段。
    return [_copy_jsonable_doc(doc) for doc in (docs or []) if isinstance(doc, dict)]


def _is_hitl_result(result: dict | None) -> bool:
    """判断图结果是否应暂停并向用户提出澄清或范围选择问题。"""
    # 防御调用方传入 None 或其他类型，非字典结果不可能是 HITL 结果。
    if not isinstance(result, dict):
        return False
    trace = result.get("rag_trace") or {}  # trace 可能还未创建，因此回退空字典。
    status = result.get("retrieval_status") or trace.get("retrieval_status")  # 优先读顶层状态。
    route = result.get("route") or trace.get("route")  # 同样兼容状态只存在 trace 的情形。
    return status in ("needs_clarification", "needs_scope_selection") or route in ("clarify", "scope_select")


# 只提取下一请求恢复所需字段，当前文档和模型对象不会跨请求保存。
def _build_hitl_resume_state(result: dict) -> dict:
    """从本轮图结果提取下一请求恢复所需的最小状态快照。"""
    trace = result.get("rag_trace") or {}  # 从 trace 补齐顶层可能未返回的字段。
    # Pydantic 在 dump 前校验路由、状态、重写次数和子问题数量。
    return HitlResumeState(
        question=result.get("question") or trace.get("query") or "",
        route=result.get("route") or trace.get("route"),
        retrieval_status=result.get("retrieval_status") or trace.get("retrieval_status"),
        rewrite_count=int(result.get("rewrite_count") or 0),
        complexity=result.get("complexity") or trace.get("complexity"),
        complexity_reason=result.get("complexity_reason") or trace.get("complexity_reason"),
        sub_questions=result.get("sub_questions") or trace.get("sub_questions") or [],
    ).model_dump()


def _refined_question_for_hitl(resume_state: dict, user_answer: str) -> str:
    """将用户补充与原问题合成为可直接重新检索的查询，避免重复拼接。"""
    question = resume_state.get("question") or ""  # 取出暂停前的原问题，缺失则使用空字符串。
    answer = user_answer.strip()  # 清除用户补充内容两端的无意义空白。
    if not question:  # 没有原问题时只能把用户补充当作完整查询。
        return answer
    if answer and answer in question:  # 原问题已经包含补充内容时避免重复拼接。
        return question
    return f"{answer}：{question}" if answer else question  # 用中文冒号把条件与原问题组合。


# 节点通过请求 Context 发送可观察步骤，但事件不会参与路由判断。
def _emit(state: RAGState, icon: str, label: str, detail: str = "") -> None:
    """通过请求级 Context 发送前端可观察的 RAG 进度，不改变图的路由状态。"""
    ctx = state["request_context"]  # 取得当前请求自己的 Context，不能使用全局对象。
    # Context 会负责线程安全地把进度事件放入 SSE 输出队列。
    ctx.emit_rag_step(
        icon,
        label,
        detail,
        group=state.get("rag_step_group"),
        group_label=state.get("rag_step_group_label"),
    )


def _initial_state(
    question: str,
    ctx: ChatRequestContext,
    *,
    is_sub_agent: bool = False,
    rag_step_group: Optional[str] = None,
    rag_step_group_label: Optional[str] = None,
    retrieval_runtime: RetrievalRuntime | None = None,
) -> dict:
    """创建首次运行或子 Agent 运行所需的完整初始图状态。

    所有可选字段均在这里显式初始化，节点无需依赖隐式的 ``dict.get`` 默认值；复杂问题
    的子 Agent 会传入自己的步骤分组，使前端能显示并行分支的进度。
    """
    # 返回完整初始字典，让所有节点都能读取到预期字段。
    return {
        "question": question,  # 保存用户原问题，后续重写也不覆盖它。
        "query": question,  # 首轮检索默认直接使用原问题。
        "context": "",  # 尚未检索，因此没有格式化证据文本。
        "docs": [],  # 尚未检索，因此候选块为空。
        "route": None,  # 尚未评分，因此没有下一跳路由。
        "retrieval_status": None,  # 尚未评分，因此没有对外状态。
        "evidence_relevance": None,  # 尚未评分，因此未知主题相关性。
        "evidence_answerability": None,  # 尚未评分，因此未知证据是否可回答。
        "evidence_ambiguity": None,  # 尚未评分，因此未知是否缺条件或存在多方向。
        "evidence_confidence": None,  # 尚未获得评分模型的置信度。
        "missing_slots": [],  # 默认没有待补充条件。
        "hitl_prompt": "",  # 默认不展示 HITL 提问。
        "hitl_options": [],  # 默认没有范围选择项。
        "rewrite_count": 0,  # 首次运行尚未使用唯一改写预算。
        "rewrite_method": None,  # 尚未选择 Step-back 或 HyDE。
        "rewritten_query": None,  # 尚未生成第二次检索查询。
        "step_back_question": None,  # 尚未产生抽象退步问题。
        "hyde_document": None,  # 尚未产生假设性检索文档。
        "initial_retrieval": None,
        "rewrite_failed": False,
        "candidate_audits": [],
        "rag_trace": None,  # 检索节点会创建本轮诊断记录。
        "complexity": None,  # 复杂度节点会写入 simple 或 complex。
        "complexity_reason": None,  # 复杂度节点会写入判断理由。
        "sub_questions": None,  # 复杂问题才会写入子问题列表。
        "is_sub_agent": is_sub_agent,  # 标记当前状态是否属于并行子分支。
        "sub_results": [],  # 主状态初始没有任何子 Agent 返回结果。
        "request_context": ctx,  # 传递同一个请求级 SSE/trace 上下文。
        "knowledge_filenames": list(ctx.knowledge_filenames),  # 子 Agent 与主流程共享同一检索范围。
        "rag_step_group": rag_step_group,  # 子分支可指定前端步骤分组 ID。
        "rag_step_group_label": rag_step_group_label,  # 子分支可指定分组显示名称。
        "retrieval_runtime": retrieval_runtime,  # 评测注入的依赖沿整张图显式传递。
    }


def _rewrite_candidate_fusion_enabled(state: RAGState) -> bool:
    runtime = state.get("retrieval_runtime")
    return bool(runtime and runtime.enable_rewrite_candidate_fusion)


def _adjacent_l3_expansion_enabled(state: RAGState) -> bool:
    """Return the evaluation-only T10 flag without changing online routing."""
    runtime = state.get("retrieval_runtime")
    return bool(runtime and runtime.enable_adjacent_l3_expansion)


def _candidate_trace_capture_enabled(state: RAGState) -> bool:
    runtime = state.get("retrieval_runtime")
    return bool(runtime and runtime.capture_candidate_trace)


def _rewrite_fusion_failure_update(state: RAGState, *, rewrite_count: int, exc: BaseException) -> RAGState:
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "rewrite_candidate_fusion_enabled": True,
        "rewrite_candidate_fusion_fallback_reason": f"rewrite_query_failed:{type(exc).__name__}",
    })
    _emit(state, "⚠️", "查询改写失败，保留首次检索证据")
    return {"rewrite_count": rewrite_count + 1, "rewrite_failed": True, "rag_trace": rag_trace}

# 第一轮只用原问题检索，并同时保存初始文档快照供 trace 对比。
def retrieve_initial(state: RAGState) -> RAGState:
    query = state["question"]  # 首轮始终用原问题检索，而非尚未存在的改写查询。
    _emit(state, "🔍", "正在检索知识库...", "初始检索")
    retrieved = retrieve_documents(
        query,
        top_k=RETRIEVAL_TOP_K,
        knowledge_filenames=state.get("knowledge_filenames"),
        runtime=state.get("retrieval_runtime"),
    )  # 调用底层召回、合并和精排流水线。
    results = retrieved.get("docs", [])  # 读取最终可用的证据块列表。
    retrieve_meta = retrieved.get("meta", {})  # 读取候选数、精排和合并等诊断数据。
    context = _format_docs(results)  # 将文档块转换为评分模型可读的带编号上下文。
    _emit(
        state,
        "🧱",
        "三级分块检索",
        (
            f"叶子层 L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}"
        ),
    )
    _emit(
        state,
        "🧩",
        "Auto-merging 合并",
        (
            f"启用: {bool(retrieve_meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(retrieve_meta.get('auto_merge_applied'))}，"
            f"替换片段: {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    _emit(state, "✅", f"检索完成，找到 {len(results)} 个片段", f"模式: {retrieve_meta.get('retrieval_mode', 'hybrid')}")
    if not results:  # 没有召回块时仍进入评分节点，由它给出确定性 no_knowledge 结果。
        _emit(state, "⚠️", "无可用片段，将进入证据评分短路判断")
    # 将初次检索输入、输出和底层白名单诊断字段一起保存。
    rag_trace = {
        "tool_used": True,  # 表示本轮确实调用了知识库检索工具。
        "tool_name": "search_knowledge_base",  # 记录工具名，便于前端和日志展示。
        "query": query,  # 保存本次实际提交给检索器的原问题。
        "retrieved_chunks": results,  # 当前用于后续评分和回答的检索块。
        "initial_retrieved_chunks": results,  # 专门保存初次召回快照，供与改写结果比较。
        "retrieval_stage": "initial",  # 标记这是首次检索而非改写或 HITL 恢复。
        "complexity": state.get("complexity"),  # 记录问题被判断为 simple 或 complex。
        "complexity_reason": state.get("complexity_reason"),  # 记录复杂度判断理由。
        **retrieval_trace_fields(retrieve_meta),  # 展开底层检索允许公开的白名单诊断字段。
    }
    # 节点只返回本节点更新的字段，LangGraph 会合并到已有 RAGState。
    update: RAGState = {
        "query": query,  # 把实际检索查询写回图状态。
        "docs": results,  # 把候选证据交给评分节点。
        "context": context,  # 把带引用编号的正文交给评分模型。
        "rag_trace": rag_trace,  # 把初次检索诊断交给后续节点继续追加。
    }
    if _rewrite_candidate_fusion_enabled(state):
        update["initial_retrieval"] = retrieved
    if _candidate_trace_capture_enabled(state):
        update["candidate_audits"] = list(retrieved.get("candidate_audits") or [])
        for audit in update["candidate_audits"]:
            for candidate in audit.get("raw_leaf_candidates") or []:
                candidate.setdefault("candidate_sources", ["initial"])
    return update


def _route_after_initial(state: RAGState) -> Literal["grade_documents"]:
    """初始检索后固定进入证据评分节点；保留函数便于 LangGraph 显式展示条件边。"""
    return "grade_documents"


def _route_after_grade(state: RAGState) -> Literal["rewrite_question", "end"]:
    """只有评分结果允许改写时进入重写节点，其他路由均结束本次主图。"""
    if state.get("route") == "rewrite":  # 仅 rewrite 路由需要继续执行图。
        # T10 intentionally keeps one original-query recall. Its only new
        # behavior is direct adjacent-L3 expansion before the existing merge
        # and rerank steps, so it must not add a rewritten query or a second
        # embedding/vector-retrieval call.
        if _adjacent_l3_expansion_enabled(state):
            return "end"
        return "rewrite_question"
    return "end"


def _retrieval_status_for_route(route: str, grade: EvidenceGrade) -> str:
    """将内部路由和评分结果转换为对外稳定的检索状态字符串。"""
    if route == "answer":  # 回答路由还需区分证据充分还是部分相关。
        if grade.answerability == "partial":  # 部分证据允许回答，但要对外标为 partial。
            return "partial"
        return "answerable"
    if route == "rewrite":  # 需要改写时检索尚未结束。
        return "needs_rewrite"
    if route == "clarify":  # 缺少条件时暂停等待用户澄清。
        return "needs_clarification"
    if route == "scope_select":  # 多个候选范围时暂停等待用户选择。
        return "needs_scope_selection"
    return "no_knowledge"


def _default_hitl_prompt(route: str, grade: EvidenceGrade) -> str:
    """优先采用评分模型的追问文案；缺失时根据路由生成保守的默认提问。"""
    if grade.hitl_prompt:  # 模型已经提供合格文案时优先使用。
        return grade.hitl_prompt
    if route == "scope_select":  # 范围选择有固定且通用的中文提示。
        return "我在知识库中找到了多个可能相关的方向。你想问的是哪一个？"
    if grade.missing_slots:  # 将评分模型识别出的缺失条件拼成可读文本。
        return "我找到了相关内容，但还缺少关键信息：" + "、".join(grade.missing_slots)
    return "我找到了相关内容，但证据不足以确定答案。请补充一下你具体想问的条件。"


def _grade_for_no_docs() -> EvidenceGrade:
    """构造无召回文档时的确定性评分，避免向模型发送空上下文。"""
    # 用合法 Pydantic 对象表达“无证据”，后续仍可复用通用路由代码。
    return EvidenceGrade(
        relevance="none",
        answerability="none",
        ambiguity="none",
        route="no_knowledge",
        confidence=1.0,
        reason="no_retrieved_documents",
    )


def _grade_for_unavailable_grader() -> EvidenceGrade:
    """评分模型故障时保守停止，避免用未验证片段生成事实性回答。"""
    return EvidenceGrade(
        relevance="weak",
        answerability="none",
        ambiguity="none",
        route="no_knowledge",
        confidence=0.0,
        reason="evidence_grading_unavailable",
    )


# 模型给出建议 route 后，代码再结合证据和 rewrite_count 收敛为允许的下一步。
def _resolve_route(grade: EvidenceGrade, state: RAGState) -> str:
    """用代码收敛模型建议的路由，保证证据门槛和重写预算始终生效。

    模型可以建议下一步，但不能绕过以下硬规则：无文档必定无知识；缺关键条件必定澄清；
    主图最多改写一次；子 Agent 不允许二次重写，以控制复杂问题的运行成本和图规模。
    """
    docs = state.get("docs") or []  # 缺失 docs 时按空列表处理，避免 None 导致条件判断失败。
    rewrite_count = int(state.get("rewrite_count") or 0)  # 读取已用改写次数，缺失时按 0。
    is_sub_agent = bool(state.get("is_sub_agent"))  # 子分支拥有更严格的成本控制规则。
    route = grade.route  # 保存模型建议，后续逐条应用代码硬约束。

    if not docs or grade.relevance == "none":  # 无召回或完全无关时不允许臆测回答。
        return "no_knowledge"

    if grade.ambiguity == "missing_slot":  # 缺少关键条件时先向用户澄清。
        return "clarify"
    if grade.ambiguity == "multiple_candidates":  # 多个方向都可能命中时让用户选择范围。
        return "scope_select"

    answer_is_supported = grade.relevance == "strong" and grade.answerability == "sufficient"  # 明确定义“证据充分”。
    if route == "answer" and answer_is_supported:  # 只有模型建议和证据门槛同时满足才回答。
        return "answer"

    # 子问题不做二次纠错。partial 证据交给 synthesis 合并，完全不可回答则停止。
    if is_sub_agent:  # 子 Agent 不再调用改写模型，避免并行分支成本成倍增加。
        if grade.answerability in ("partial", "sufficient"):  # 部分证据可交给主合成节点与其他子证据组合。
            return "answer"
        return "no_knowledge"

    if route == "rewrite" and rewrite_count < 1:  # 只有尚未改写时才接受模型的改写建议。
        return "rewrite"

    if route == "rewrite" and rewrite_count >= 1:  # 第二次改写请求被预算规则拒绝。
        if grade.answerability == "partial":  # 仍有相关线索时转而请用户补充条件。
            return "clarify"
        return "no_knowledge"

    if grade.answerability == "partial":  # 即使模型未明确建议改写，部分证据也触发保守补救流程。
        if rewrite_count < 1:  # 首次不足时仍允许尝试一次查询改写。
            return "rewrite"
        return "clarify"

    if answer_is_supported:  # 兼容模型 route 不够准确但证据实际已充分的情况。
        return "answer"

    return "no_knowledge"


def _grade_update(grade: EvidenceGrade, route: str) -> dict:
    """把评分对象转换为可合并进 RAGState 与 rag_trace 的普通字典。"""
    status = _retrieval_status_for_route(route, grade)  # 将内部路由转换成 API 使用的稳定状态。
    hitl_prompt = _default_hitl_prompt(route, grade) if route in ("clarify", "scope_select") else ""  # 只有 HITL 路由才需要追问文案。
    # 返回纯字典，既可更新 State，也可直接 merge 到 rag_trace。
    return {
        "retrieval_status": status,  # API 使用的稳定状态名。
        "evidence_relevance": grade.relevance,  # 模型给出的主题相关性。
        "evidence_answerability": grade.answerability,  # 模型给出的证据充分程度。
        "evidence_ambiguity": grade.ambiguity,  # 模型给出的歧义类别。
        "evidence_confidence": grade.confidence,  # 模型给出的 0 到 1 置信度。
        "evidence_reason": grade.reason,  # 本次评分的文字原因。
        "missing_slots": grade.missing_slots,  # 用户仍需补充的条件列表。
        "hitl_prompt": hitl_prompt,  # 需要用户介入时展示的问题。
        "hitl_options": grade.hitl_options,  # 用户可选择的候选范围。
        "route": route,  # 经过硬规则修正后的最终下一跳。
    }


# 先评分再路由；没有评分模型或没有文档时使用明确的保守结果。
def grade_documents_node(state: RAGState) -> RAGState:
    """评估当前检索证据，并返回由硬规则确认后的路由及 trace 更新。

    没有文档时直接使用确定性 ``no_knowledge`` 评分；有文档时必须配置 GRADE_MODEL，
    使“证据是否足够”的判断不会退化为无依据的默认肯定回答。
    """
    _emit(state, "📊", "正在评估证据质量...")
    docs = state.get("docs") or []  # 缺少候选时按空列表走确定性分支。
    if not docs:  # 空上下文不需要调用模型，也不能被模型误判为证据充分。
        grade = _grade_for_no_docs()  # 构造 no_knowledge 的结构化评分。
    else:
        grader = _get_grader_model()  # 获取或懒创建证据评分客户端。
        if not grader:  # 已有文档却无法评分时属于配置错误，不应静默回答。
            raise RuntimeError("GRADE_MODEL is required for evidence grading")
        question = state["question"]  # 评分永远以用户原问题作为判断目标。
        context = state.get("context", "")  # 使用检索节点格式化后的带引用上下文。
        prompt = EVIDENCE_GRADE_PROMPT.format(question=question, context=context)  # 注入运行时问题和证据。
        try:
            grade = invoke_structured_output(  # 要求模型返回 EvidenceGrade 而非自由文本。
                grader,
                EvidenceGrade,
                [{"role": "user", "content": prompt}],
            )
        except Exception:
            # 兼容模型偶发忽略 function calling 时，不能让一次格式错误变成问答接口 500。
            # 无法可靠评分时必须 fail closed，禁止把候选片段直接交给回答模型。
            grade = _grade_for_unavailable_grader()
            _emit(state, "⚠️", "证据评分暂不可用，已保守停止回答")

    route = _resolve_route(grade, state)  # 对模型建议执行证据门槛和预算约束。
    grade_update = _grade_update(grade, route)  # 转换成状态/trace 可直接使用的字段。
    rag_trace = state.get("rag_trace", {}) or {}  # 延续初始检索或重写检索已建立的 trace。
    rag_trace.update(grade_update)  # 将此次评分与最终路由写进诊断记录。

    if route == "answer":  # 回答路由按证据充分程度发送不同进度文案。
        if grade.answerability == "partial":  # 部分证据可能由复杂问题合成流程补全。
            _emit(state, "🟡", "保留部分相关证据", f"置信度: {grade.confidence:.2f}")
        else:
            _emit(state, "✅", "证据足够，返回检索片段", f"置信度: {grade.confidence:.2f}")
    elif route == "rewrite":  # 仅提示即将改写，实际改写由下一节点完成。
        _emit(state, "⚠️", "证据不足，将改写查询一次", f"置信度: {grade.confidence:.2f}")
    elif route in ("clarify", "scope_select"):  # HITL 路由把追问文本作为前端步骤详情。
        _emit(state, "❓", "需要用户补充信息", grade_update["hitl_prompt"])
    else:  # 剩余路由为 no_knowledge。
        _emit(state, "⛔", "知识库中未找到可用证据", grade.reason or "no_knowledge")

    # 构造本节点需要交给 LangGraph 合并的状态更新。
    update = {
        "route": route,
        "retrieval_status": grade_update["retrieval_status"],
        "evidence_relevance": grade.relevance,
        "evidence_answerability": grade.answerability,
        "evidence_ambiguity": grade.ambiguity,
        "evidence_confidence": grade.confidence,
        "missing_slots": grade.missing_slots,
        "hitl_prompt": grade_update["hitl_prompt"],
        "hitl_options": grade.hitl_options,
        "rag_trace": rag_trace,
    }

    if route in ("no_knowledge", "clarify", "scope_select"):  # 这些路由不应把当前文档直接交给回答模型。
        if route in ("clarify", "scope_select") and docs:  # 仍保留内部证据用于 trace，但从公开 retrieved_chunks 隐藏。
            rag_trace["retrieved_chunks"] = []
        update.update({"docs": [], "context": ""})

    return update

# 进入重写节点即消费唯一预算，失败或第二次进入都必须转向终止分支。
def rewrite_question_node(state: RAGState) -> RAGState:
    """消耗唯一的查询改写预算，并验证 Step-back/HyDE 返回的互斥结构。"""
    question = state["question"]  # Step-back/HyDE 都基于原始用户问题规划。
    _emit(state, "✏️", "正在重写查询...")

    rewrite_count = int(state.get("rewrite_count") or 0)  # 读取唯一改写预算已用次数。
    if rewrite_count >= 1:  # 防御图意外重入，第二次改写一律停止。
        rag_trace = state.get("rag_trace", {}) or {}  # 保留已有诊断内容并补充终止原因。
        rag_trace.update({
            "retrieval_status": "no_knowledge",
            "route": "no_knowledge",
            "evidence_reason": "rewrite_budget_exhausted",
        })
        _emit(state, "⛔", "改写预算已用完，停止检索")
        return {
            "route": "no_knowledge",
            "retrieval_status": "no_knowledge",
            "docs": [],
            "context": "",
            "rag_trace": rag_trace,
        }

    _emit(state, "🧠", "选择 Step-back / HyDE 重写方式")
    try:
        rewrite = rewrite_query_once(question)  # 调用 utils 中的结构化二选一重写能力。
    except Exception as exc:
        if not _rewrite_candidate_fusion_enabled(state):
            raise
        return _rewrite_fusion_failure_update(state, rewrite_count=rewrite_count, exc=exc)
    rewrite_method = (rewrite.get("rewrite_method") or "").strip()  # 读取并清洗策略名称。
    step_back_question = (rewrite.get("step_back_question") or "").strip()  # 读取 Step-back 输出。
    hyde_document = (rewrite.get("hyde_document") or "").strip()  # 读取 HyDE 输出。
    rewritten_query = (rewrite.get("rewritten_query") or "").strip()  # 读取真正用于检索的拼接查询。
    if rewrite_method not in ("step_back", "hyde") or not rewritten_query:
        exc = ValueError("Query rewriting returned an incomplete result")
        if _rewrite_candidate_fusion_enabled(state):
            return _rewrite_fusion_failure_update(state, rewrite_count=rewrite_count, exc=exc)
        raise exc
    if rewrite_method == "step_back" and (not step_back_question or hyde_document):
        exc = ValueError("Step-back rewriting returned an invalid result")
        if _rewrite_candidate_fusion_enabled(state):
            return _rewrite_fusion_failure_update(state, rewrite_count=rewrite_count, exc=exc)
        raise exc
    if rewrite_method == "hyde" and (not hyde_document or step_back_question):
        exc = ValueError("HyDE rewriting returned an invalid result")
        if _rewrite_candidate_fusion_enabled(state):
            return _rewrite_fusion_failure_update(state, rewrite_count=rewrite_count, exc=exc)
        raise exc

    method_label = "Step-back" if rewrite_method == "step_back" else "HyDE"  # 将内部枚举转成前端可读名称。
    _emit(state, "✅", f"已选择 {method_label} 重写", "本轮只执行这一种重写检索")

    rag_trace = state.get("rag_trace", {}) or {}  # 继续记录同一轮检索的诊断过程。
    rag_trace.update({
        "rewrite_method": rewrite_method,
        "rewritten_query": rewritten_query,
        "rewrite_count": rewrite_count + 1,
    })
    if step_back_question:  # 仅在使用 Step-back 时写入对应字段。
        rag_trace["step_back_question"] = step_back_question
    if hyde_document:  # 仅在使用 HyDE 时写入假设文档字段。
        rag_trace["hyde_document"] = hyde_document

    return {
        "rewrite_method": rewrite_method,  # 保存实际采用的 Step-back 或 HyDE 方法。
        "rewritten_query": rewritten_query,  # 保存下一节点必须使用的改写查询。
        "step_back_question": step_back_question,  # 保存抽象后的退步问题；HyDE 时为空。
        "hyde_document": hyde_document,  # 保存假设文档；Step-back 时为空。
        "rewrite_count": rewrite_count + 1,  # 消耗一次改写预算，阻止后续再次改写。
        "rag_trace": rag_trace,  # 将改写计划加入同一轮诊断记录。
    }


# 重写后只再检索一次，结果回到评分节点时 rewrite_count 已阻止继续循环。
def retrieve_rewritten(state: RAGState) -> RAGState:
    """使用已生成的改写查询再次检索，并覆盖当前候选文档和上下文。

    重写方法和查询均由上一节点写入状态；缺失时立即报错，避免错误地用空查询或原问题
    进行“重写后检索”。评分节点随后会看到 rewrite_count=1，因此不能再次循环改写。
    """
    fusion_enabled = _rewrite_candidate_fusion_enabled(state)
    initial_retrieval = state.get("initial_retrieval") or {"docs": state.get("docs") or [], "meta": {}}
    if state.get("rewrite_failed"):
        fallback_reason = (state.get("rag_trace") or {}).get("rewrite_candidate_fusion_fallback_reason")
        fused = fuse_rewrite_candidate_results(
            original_query=state["question"], initial_retrieval=initial_retrieval,
            rewritten_retrieval=None, top_k=RETRIEVAL_TOP_K,
            runtime=state.get("retrieval_runtime"), fallback_reason=str(fallback_reason or "rewrite_query_failed"),
        )
        results = fused.get("docs", [])
        rag_trace = state.get("rag_trace", {}) or {}
        rag_trace.update({"retrieved_chunks": results, "rewrite_retrieved_chunks": [], "retrieval_stage": "rewrite_fusion_fallback", **retrieval_trace_fields(fused.get("meta", {}))})
        return {"docs": results, "context": _format_docs(results), "candidate_audits": fused.get("candidate_audits") or [], "rag_trace": rag_trace}
    rewrite_method = (state.get("rewrite_method") or "").strip()  # 读取上个节点已验证的改写方法。
    if rewrite_method not in ("step_back", "hyde"):
        raise ValueError("rewrite_method is required for rewritten retrieval")
    rewritten_query = (state.get("rewritten_query") or "").strip()  # 读取上个节点生成的第二次检索查询。
    if not rewritten_query:
        raise ValueError("rewritten_query is required for rewritten retrieval")
    method_label = "Step-back" if rewrite_method == "step_back" else "HyDE"  # 用于 SSE 展示。
    _emit(state, "🔄", f"使用 {method_label} 查询重新检索...")
    retrieved = retrieve_documents(
        rewritten_query,
        top_k=RETRIEVAL_TOP_K,
        knowledge_filenames=state.get("knowledge_filenames"),
        runtime=state.get("retrieval_runtime"),
    )  # 用改写查询重新执行完整检索流水线。
    for audit in retrieved.get("candidate_audits") or []:
        audit["query_origin"] = rewrite_method
        audit["query"] = rewritten_query
        for candidate in audit.get("raw_leaf_candidates") or []:
            candidate.setdefault("candidate_sources", [rewrite_method])
    rewritten_results = retrieved.get("docs", [])  # 提取改写后的最终候选证据。
    if fusion_enabled:
        fused = fuse_rewrite_candidate_results(
            original_query=state["question"], initial_retrieval=initial_retrieval,
            rewritten_retrieval=retrieved, top_k=RETRIEVAL_TOP_K,
            runtime=state.get("retrieval_runtime"),
        )
        results = fused.get("docs", [])
        retrieve_meta = fused.get("meta", {})
        candidate_audits = list(state.get("candidate_audits") or [])
        if fused.get("candidate_audit"):
            candidate_audits.append(fused["candidate_audit"])
    else:
        results = rewritten_results
        retrieve_meta = retrieved.get("meta", {})
        candidate_audits = []
    context = _format_docs(results)  # 重新生成供第二次评分使用的引用上下文。
    _emit(
        state,
        "🧱",
        f"{method_label} 三级检索",
        (
            f"L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}，"
            f"合并替换 {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    _emit(state, "✅", f"重写检索完成，共 {len(results)} 个片段")
    rag_trace = state.get("rag_trace", {}) or {}  # 保留初次检索与改写计划的 trace。
    rag_trace.update({
        "rewrite_method": rewrite_method,
        "rewritten_query": rewritten_query,
        "retrieved_chunks": results,
        "rewrite_retrieved_chunks": rewritten_results,
        "retrieval_stage": "rewrite_fusion" if fusion_enabled and retrieve_meta.get("rewrite_candidate_fusion_applied") else "rewritten",
        **retrieval_trace_fields(retrieve_meta),
    })
    if state.get("step_back_question"):  # 将前一节点的 Step-back 细节带入最终 trace。
        rag_trace["step_back_question"] = state["step_back_question"]
    if state.get("hyde_document"):  # 将前一节点的 HyDE 细节带入最终 trace。
        rag_trace["hyde_document"] = state["hyde_document"]
    return {
        "docs": results,  # 使用改写查询得到的新证据，覆盖首次候选。
        "context": context,  # 使用新证据生成的评分上下文。
        "candidate_audits": candidate_audits,
        "rag_trace": rag_trace,  # 同时保留初次和改写检索的诊断。
    }


# ---------------------------------------------------------------------------
# 复杂度分类 & 子问题分解
# ---------------------------------------------------------------------------
COMPLEXITY_PROMPT = (
    "你是一个问题复杂度规划器。请判断用户问题的复杂度。\n\n"
    "【简单问题】：事实查询、定义查询、单一信息点查询、明确的二选一问题、"
    "某个具体属性/参数/规格的查询。\n"
    "【复杂问题】：需要跨文档综合、多角度分析、比较对比、多步骤推理、"
    "需要综合多个信息源才能完整回答的问题。\n\n"
    "用户问题：{question}\n\n"
    "如果是复杂问题，请同时给出 2-4 个互不重叠、可独立检索的子问题；"
    "如果是简单问题，sub_questions 留空。"
)

_PRESERVE_INPUT_LANGUAGE_PROMPT = (
    "\n\n【子问题语言规则】\n"
    "sub_questions 必须与用户问题使用相同语言。用户问题是英文时，所有 sub_questions 必须是英文；"
    "不要把英文问题翻译成中文。保留原问题中的人名、产品名、区域、版本号、数字和时间。"
)


def _complexity_prompt(question: str, runtime: RetrievalRuntime | None) -> str:
    """Build the planner prompt without changing the legacy online default."""
    prompt = COMPLEXITY_PROMPT.format(question=question)
    if runtime and runtime.subquestion_language_policy == "preserve_input_language_v1":
        return prompt + _PRESERVE_INPUT_LANGUAGE_PROMPT
    return prompt

# 关键词规则只用于高置信度的简单问题快速放行；复杂度模型仍是兜底判断来源。
_SIMPLE_QUERY_MARKERS = (
    "是什么",
    "是谁",
    "哪里",
    "何时",
    "多少",
    "是否",
    "哪个",
    "哪种",
    "属性",
    "参数",
    "规格",
    "定义",
    "含义",
    "what is",
    "who is",
    "where is",
    "when is",
    "how many",
    "which",
)

_COMPLEX_QUERY_MARKERS = (
    "比较",
    "对比",
    "区别",
    "差异",
    "优缺点",
    "优势",
    "劣势",
    "分析",
    "总结",
    "综合",
    "原因",
    "成因",
    "影响",
    "方案",
    "步骤",
    "如何",
    "为什么",
    "以及",
    "同时",
    "并且",
    "和",
    "与",
    "谁更",
    "compare",
    "versus",
    "difference",
    "different",
    "analyze",
    "summarize",
    "trade-off",
    "pros and cons",
    "why ",
    "how ",
    "complex",
)

_QUERY_DIMENSION_MARKERS = (
    "属性",
    "武器",
    "定位",
    "技能",
    "机制",
    "参数",
    "规格",
    "性能",
    "价格",
    "优点",
    "缺点",
    "作用",
)


# 明显简单的单意图问题由确定性规则快速通过，节省一次复杂度模型调用。
def _simple_question_fast_path_reason(question: str) -> Optional[str]:
    """仅在本地规则能高置信度认定“单意图简单问题”时返回原因。

    多维度、多个分隔问题、混合中英文短语或含复杂关键词时会主动放弃快速路径，交给模型
    分类。这样规则只节省明显简单请求的模型调用，而不抢占复杂问题的规划机会。
    """
    normalized = re.sub(r"\s+", " ", (question or "").strip()).lower()  # 统一空白并转小写，便于中英文关键词匹配。
    if not normalized or len(normalized) > 48:  # 空问题或过长问题不适合用简单规则判断。
        return None
    if any(marker in normalized for marker in _COMPLEX_QUERY_MARKERS):  # 发现比较、分析等词时交给模型。
        return None
    if "、" in normalized:  # 顿号通常表示多个并列意图，不能快速判为简单。
        return None
    if re.search(r"[\u4e00-\u9fff]", normalized) and normalized.count(" ") >= 2:  # 中文夹杂多个空格词组时可能是复合问题。
        return None
    if sum(marker in normalized for marker in _QUERY_DIMENSION_MARKERS) >= 2:  # 同时问两个维度时不走快速路径。
        return None
    if sum(normalized.count(mark) for mark in ("?", "？", ";", "；")) > 1:  # 多个问号或分号暗示多个问题。
        return None
    if any(marker in normalized for marker in _SIMPLE_QUERY_MARKERS):  # 明显事实问法可直接归为简单。
        return "obvious_simple_fast_path:single_fact_marker"
    if len(normalized.rstrip("?？。.!！")) <= 18:  # 极短单句在未命中复杂信号时按简单处理。
        return "obvious_simple_fast_path:short_single_intent"
    return None


# 只有快速规则无法判断时才调用模型，并把原因写入 trace。
def classify_complexity(state: RAGState) -> RAGState:
    """先尝试确定性快速路径，必要时使用 FAST_MODEL 判断复杂度并规划子问题。"""
    question = state["question"]  # 分类始终以用户原问题为依据。
    _emit(state, "🧭", "正在分析问题复杂度...")

    fast_path_reason = _simple_question_fast_path_reason(question)  # 先尝试无需模型调用的确定性判断。
    if fast_path_reason:  # 返回非空理由表示高置信度简单问题。
        _emit(state, "⚡", "快速判断为简单问题 → 走标准 RAG 流程")
        return {"complexity": "simple", "complexity_reason": fast_path_reason}

    model = _get_complexity_model()  # 获取或延迟创建 FAST_MODEL 客户端。
    if not model:  # 无法规划复杂度时明确提示缺失配置。
        raise RuntimeError("FAST_MODEL is required for complexity planning")

    prompt = _complexity_prompt(question, state.get("retrieval_runtime"))
    result = invoke_structured_output(  # 强制模型返回 ComplexityResult 结构。
        model,
        ComplexityResult,
        [{"role": "user", "content": prompt}]
    )
    complexity = (result.complexity or "simple").strip().lower()  # 清洗复杂度枚举。
    reason = (result.reason or "").strip()  # 清洗模型解释文本。
    sub_questions = [
        item.strip()
        for item in (result.sub_questions or [])
        if item and item.strip()
    ][:4]
    if complexity not in ("simple", "complex"):  # 防御未来模型或 Schema 变更产生未知值。
        raise ValueError(f"Unsupported complexity result: {complexity}")
    if complexity == "complex" and not sub_questions:  # 复杂路径没有子问题将无法扇出，直接报错。
        raise ValueError("Complexity planner returned no sub-questions")

    if complexity == "simple":
        _emit(state, "✅", "简单问题 → 走标准 RAG 流程", f"理由: {reason[:60]}")
    else:
        _emit(state, "🔀", "复杂问题 → 将分解为子问题并行检索", f"理由: {reason[:60]}")

    return {
        "complexity": complexity,  # 分类结论：simple 走主路径，complex 进入并行拆题。
        "complexity_reason": reason,  # 模型或规则作出该结论的理由。
        "sub_questions": sub_questions if complexity == "complex" else [],  # 简单问题明确返回空列表。
    }


# 复杂问题的子问题在此清理空白项；数量上限由 Pydantic Schema 和分类节点共同约束。
def prepare_sub_questions(state: RAGState) -> RAGState:
    """清理复杂度规划器产出的子问题，并将每个并行任务通知给前端。"""
    # 清除空项和首尾空白，保留规划器给出的原有顺序。
    planned_sub_questions = [
        item.strip()
        for item in (state.get("sub_questions") or [])
        if item and item.strip()
    ]
    for i, sq in enumerate(planned_sub_questions, 1):  # 逐个通知前端即将并行执行的分支。
        _emit(state, "📌", f"子问题 {i}", f"{sq[:80]} 已加入并行检索")
    return {"sub_questions": planned_sub_questions}  # 将清洗后的子问题写回图状态。


def _route_after_complexity(state: RAGState):
    """简单问题直接检索，复杂问题先准备子问题再通过 Send 并行分发。"""
    # T10 keeps exactly one original-question Hybrid candidate pool. Complex
    # decomposition would add embeddings/vector searches and break that bound.
    if _adjacent_l3_expansion_enabled(state):
        return "retrieve_initial"
    if state.get("complexity") == "complex":  # 复杂问题先准备子问题，再由 Send 扇出。
        return "prepare_sub_questions"
    return "retrieve_initial"


# 每个 Send 获得独立子问题状态，子图结果最终统一回到 synthesis。
def _fanout_sub_questions(state: RAGState):
    """为每个子问题创建独立初始状态，并通过 Send API 并行派发给子 Agent。

    子 Agent 共用同一个请求 Context，因此 SSE 仍属于同一次聊天请求；但各自拥有独立
    的 question、docs、trace 和步骤分组，彼此不会覆盖状态。
    """
    sub_qs = state.get("sub_questions") or []  # 未提供子问题时按空列表处理。
    ctx = state["request_context"]  # 所有分支共享此次请求的 SSE 上下文。
    # 列表推导式为每个子问题构造一个 Send 指令。
    return [
        Send(
            "rag_sub_agent",
            _initial_state(
                sq,
                ctx,
                is_sub_agent=True,
                rag_step_group=f"子问题 {i}",
                rag_step_group_label=sq,
                retrieval_runtime=state.get("retrieval_runtime"),
            ),
        )
        for i, sq in enumerate(sub_qs, 1)
    ]

# 合成阶段按子问题顺序合并文档并去重，这个顺序决定最终引用编号。
def synthesis(state: RAGState) -> RAGState:
    """合并所有子 Agent 的有效证据，去重后生成最终上下文和主 trace。

    只有 ``answerable`` 或 ``partial`` 子结果参与文档合成；随后保持子问题返回顺序去重，
    再重建 ``rrf_rank``，使最终上下文中的引用编号稳定可解释。
    """
    sub_results = state.get("sub_results", [])  # 读取 reducer 已聚合的所有子分支结果。
    _emit(state, "🔬", f"正在合成 {len(sub_results)} 个子问题的检索结果...")

    all_docs: List[dict] = []  # 收集各子问题的可用文档，稍后统一去重。
    for result in sub_results:  # 按子问题返回顺序遍历。
        status = result.get("retrieval_status")  # 读取该分支的证据状态。
        if status not in ("answerable", "partial"):  # 无知识或 HITL 分支不提供回答证据。
            continue
        docs = result.get("docs", [])  # 读取该子问题的文档块。
        all_docs.extend(docs)  # 追加到总候选池，保留分支顺序。

    deduped = dedupe_documents(all_docs)  # 对相同 chunk_id 合并，避免最终引用重复。
    for idx, item in enumerate(deduped, 1):  # 按合成后的顺序重建连续引用排名。
        item["rrf_rank"] = idx

    context = _format_docs(deduped)  # 生成最终回答模型可使用的带编号上下文。
    if deduped:  # 根据是否有证据发送不同的前端进度事件。
        _emit(state, "✅", f"合成完成，共 {len(deduped)} 个去重片段")
    else:
        _emit(state, "⛔", "所有子问题都没有可用证据")

    # 每个子 trace 再走一次白名单规范化，主 trace 不直接信任并行分支的临时字段。
    sub_traces = []  # 存放规范化后的子 Agent 诊断记录。
    candidate_audits: List[dict] = []
    for result in sub_results:  # 每个并行结果都可能携带自己的 trace。
        candidate_audits.extend(result.get("candidate_audits") or [])
        trace = result.get("rag_trace")  # 读取原始子 trace。
        if trace:  # 只有非空 trace 才需要 Pydantic 规范化。
            normalized_trace = normalize_rag_sub_trace(trace)  # 删除未知字段并校验结构。
            if normalized_trace:  # 规范化可能因空内容返回 None。
                sub_traces.append(normalized_trace)

    original_trace = state.get("rag_trace") or {}  # 保留主图在分类阶段已建立的诊断信息。
    has_docs = bool(deduped)  # 文档是否存在决定回答或无知识的主路由。
    retrieval_status = "answerable" if has_docs else "no_knowledge"
    if has_docs and any(result.get("retrieval_status") == "partial" for result in sub_results):  # 任一分支部分相关时如实标记总体不完整。
        retrieval_status = "partial"
    # 若没有任何可用文档，但某个子问题需要澄清，则把该 HITL 信息上浮给主流程。
    hitl_traces = [
        trace for trace in sub_traces
        if trace.get("retrieval_status") in ("needs_clarification", "needs_scope_selection")
    ]
    hitl_route = None
    hitl_prompt = ""
    hitl_options: List[str] = []
    if not has_docs and hitl_traces:  # 只有没有可回答文档时才将子分支 HITL 上浮。
        scope_trace = next(  # 优先范围选择，因为其通常比普通澄清提供更明确选项。
            (trace for trace in hitl_traces if trace.get("retrieval_status") == "needs_scope_selection"),
            None,
        )
        chosen_trace = scope_trace or hitl_traces[0]  # 没有范围选择时使用第一个澄清 trace。
        retrieval_status = chosen_trace.get("retrieval_status") or "needs_clarification"
        hitl_route = "scope_select" if retrieval_status == "needs_scope_selection" else "clarify"
        prompts = [  # 收集所有非空追问，随后保持顺序去重。
            trace.get("hitl_prompt")
            for trace in hitl_traces
            if trace.get("hitl_prompt")
        ]
        hitl_prompt = "；".join(dict.fromkeys(prompts))  # dict.fromkeys 保持插入顺序并删除重复文案。
        for trace in hitl_traces:  # 汇总各子问题给出的候选选项。
            for option in trace.get("hitl_options") or []:  # 缺失选项时按空列表处理。
                if option not in hitl_options:  # 避免前端展示重复选项。
                    hitl_options.append(option)

    rag_trace = {
        **original_trace,  # 保留主图在复杂度分类阶段已经写入的字段。
        "tool_used": True,  # 子 Agent 虽并行运行，整体仍只记录为知识库工具调用。
        "tool_name": "search_knowledge_base",  # 顶层工具名称。
        "query": state["question"],  # 顶层问题，而非某一个子问题。
        "retrieved_chunks": deduped,  # 去重后的最终可引用证据块。
        "retrieval_stage": "synthesis",  # 标记当前结果来自子问题合成阶段。
        "complexity": "complex",  # 进入 synthesis 即可确认这是复杂问题路径。
        "complexity_reason": state.get("complexity_reason", ""),  # 继承原分类理由。
        "sub_questions": state.get("sub_questions", []),  # 保存实际拆出的子问题。
        "sub_agent_count": len(sub_results),  # 记录实际返回结果的子 Agent 数量。
        "synthesis_merged_count": len(all_docs),  # 去重前参与合成的文档数量。
        "sub_traces": sub_traces,  # 每个子 Agent 的规范化诊断记录。
        "retrieval_status": retrieval_status,  # 合成后的整体证据状态。
        "evidence_relevance": "strong" if has_docs else "none",  # 有合成证据时视为整体主题相关。
        "evidence_answerability": "partial" if retrieval_status == "partial" else ("sufficient" if has_docs else "none"),  # 描述整体可回答程度。
        "evidence_confidence": None,  # 合成阶段不重新让模型给单一置信度。
        "route": "answer" if has_docs else (hitl_route or "no_knowledge"),  # 有证据则回答，否则上浮 HITL 或无知识。
        "hitl_prompt": hitl_prompt,  # 汇总后的澄清提问。
        "hitl_options": hitl_options,  # 汇总且去重后的选择项。
    }

    return {
        "docs": deduped,  # 最终交给回答模型的去重证据。
        "context": context,  # 最终带 [1]、[2] 编号的证据文本。
        "route": "answer" if has_docs else (hitl_route or "no_knowledge"),  # 主图的最终路由。
        "retrieval_status": retrieval_status,  # 主图的最终公开检索状态。
        "hitl_prompt": hitl_prompt,  # 无法回答时给前端的追问。
        "hitl_options": hitl_options,  # 无法回答时给前端的可选范围。
        "rag_trace": rag_trace,  # 完整的复杂问题诊断记录。
        "candidate_audits": candidate_audits,
    }


# 每个子问题运行同一条受预算约束的 RAG 图，并返回最小子 trace。
def rag_sub_agent(state: RAGState) -> RAGState:
    """执行复杂问题的最小子 Agent 路径：检索 -> 评分，然后返回最小结果。

    子问题不允许进入改写节点。若证据部分相关，交给 synthesis 与其他子问题合成；若没有
    证据则停止，避免每个分支继续调用模型或再次扇出。
    """
    question = state.get("question", "")  # 记录子问题文本，便于主 trace 对应回分支。
    result = dict(state)  # 复制状态，避免直接改写 LangGraph 传入的分支对象。
    result.update(retrieve_initial(result))  # 在本分支执行初次检索并合并节点更新。
    result.update(grade_documents_node(result))  # 对本分支证据评分并合并路由更新。
    trace = result.get("rag_trace") or {}  # 读取子分支最终诊断记录。
    # 子节点只向 reducer 返回最小结果，主节点负责统一合成。
    return {
        "sub_results": [{
            "question": question,
            "docs": result.get("docs", []),
            "retrieval_status": result.get("retrieval_status") or trace.get("retrieval_status"),
            "route": result.get("route") or trace.get("route"),
            "rag_trace": trace,
            "candidate_audits": result.get("candidate_audits") or [],
        }],
    }


# ---------------------------------------------------------------------------
# 主 RAG 图
# ---------------------------------------------------------------------------

# 节点、条件边和所有终点在这里显式注册，便于检查图是否可能悬空或循环。
def build_rag_graph():
    """注册主 RAG 图的节点和边，并编译为可调用的 LangGraph 应用。

    图有两条互斥主线：简单问题走单次检索与至多一次改写；复杂问题通过 ``Send`` 扇出
    子 Agent，全部完成后进入 synthesis。所有可达终点都指向 ``END``，避免意外循环。
    """
    graph = StateGraph(RAGState)  # 用 TypedDict 状态契约创建尚未编译的图定义。

    # 节点注册
    graph.add_node("classify_complexity", classify_complexity)  # 入口复杂度判断节点。
    graph.add_node("prepare_sub_questions", prepare_sub_questions)  # 复杂问题的子问题清理节点。
    graph.add_node("retrieve_initial", retrieve_initial)  # 原问题检索节点。
    graph.add_node("grade_documents", grade_documents_node)  # 证据评分与路由节点。
    graph.add_node("rewrite_question", rewrite_question_node)  # 唯一一次查询改写节点。
    graph.add_node("retrieve_rewritten", retrieve_rewritten)  # 改写后第二次检索节点。
    graph.add_node("rag_sub_agent", rag_sub_agent)  # 每个并行子问题执行的最小节点。
    graph.add_node("synthesis", synthesis)  # 合并并行子结果的终结节点。

    # 入口：复杂度分类
    graph.set_entry_point("classify_complexity")  # 所有首次问题都从复杂度判断开始。

    # 简单问题直接检索；复杂问题使用规划器一次产出的子问题。
    graph.add_conditional_edges(
        "classify_complexity",
        _route_after_complexity,
        {
            "retrieve_initial": "retrieve_initial",
            "prepare_sub_questions": "prepare_sub_questions",
        },
    )

    graph.add_conditional_edges("prepare_sub_questions", _fanout_sub_questions)  # 返回 Send 列表以并行启动子 Agent。

    # 简单问题路径
    graph.add_edge("retrieve_initial", "grade_documents")  # 初次检索完成后必须评分。
    graph.add_conditional_edges(
        "grade_documents",
        _route_after_grade,
        {
            "rewrite_question": "rewrite_question",
            "end": END,
        },
    )
    graph.add_edge("rewrite_question", "retrieve_rewritten")  # 改写完成后立即用新查询检索。
    graph.add_edge("retrieve_rewritten", "grade_documents")  # 重写检索仍需重新评分。

    # 并行子 Agent → 合成
    graph.add_edge("rag_sub_agent", "synthesis")  # 所有并行分支都汇合到合成节点。
    graph.add_edge("synthesis", END)  # 合成完成即结束复杂问题路径。

    return graph.compile()  # 校验并编译节点和边，得到可 invoke 的图应用。


# 模块加载时编译图结构；模型客户端本身仍由各节点按需延迟创建。
rag_graph = build_rag_graph()

# 恢复状态从保存的 route、status 和 rewrite_count 重建，不重新执行 Agent 工具选择。
def _state_from_resume(
    resume_state: dict,
    user_answer: str,
    ctx: ChatRequestContext,
) -> dict:
    """从已持久化 HITL 快照和用户补充重建一次定向检索的初始状态。

    恢复不重新执行主图的复杂度分类、工具选择或子问题扇出，只保留原问题的必要上下文并
    标记 trace，方便前端和消息历史解释“这次回答是在澄清后继续得到的”。
    """
    current_resume_state = HitlResumeState.model_validate(resume_state).model_dump()  # 校验数据库取回的恢复快照。
    refined_question = _refined_question_for_hitl(current_resume_state, user_answer)  # 合成用户补充与原问题。
    rag_trace = {  # 从恢复开始就记录 HITL 来源，避免与首次检索混淆。
        "tool_used": True,  # 恢复流程仍会调用知识库检索。
        "tool_name": "search_knowledge_base",  # 记录恢复时使用的工具名。
        "query": refined_question,  # 保存“用户补充 + 原问题”组合后的定向查询。
        "hitl_resumed": True,  # 明确标记本次不是首次运行。
        "hitl_answer": user_answer,  # 保存用户在暂停点提供的补充内容。
        "hitl_resume_from_status": current_resume_state["retrieval_status"],  # 记录暂停前的证据状态。
        "hitl_resume_from_route": current_resume_state["route"],  # 记录暂停前的路由。
    }
    if current_resume_state.get("complexity"):  # 仅在原状态存在时延续复杂度诊断。
        rag_trace["complexity"] = current_resume_state["complexity"]
    if current_resume_state.get("complexity_reason"):  # 延续原始分类理由。
        rag_trace["complexity_reason"] = current_resume_state["complexity_reason"]
    if current_resume_state.get("sub_questions"):  # 延续原复杂问题的子问题记录。
        rag_trace["sub_questions"] = current_resume_state["sub_questions"]
    state = _initial_state(refined_question, ctx)  # 创建干净状态，不恢复旧文档或模型对象。
    state.update({
        "query": refined_question,  # 写入恢复时真正要检索的查询。
        "rewrite_count": current_resume_state["rewrite_count"],  # 继承已用预算，防止恢复后绕过改写限制。
        "complexity": current_resume_state.get("complexity"),  # 延续原始复杂度结论。
        "complexity_reason": current_resume_state.get("complexity_reason"),  # 延续原始判断理由。
        "sub_questions": current_resume_state.get("sub_questions") or [],  # 延续拆题记录，仅用于 trace。
        "rag_trace": rag_trace,  # 将 HITL 来源写入新的运行状态。
    })
    return state


# 用户补充条件只触发针对性检索，并在 trace 中标记 hitl_targeted_retrieval。
def _retrieve_resume_query(state: dict) -> dict:
    """使用用户补充后的问题定向检索、评分，并把结果标记为 HITL 恢复阶段。"""
    _emit(state, "🔎", "使用 HITL 补充进行针对性检索", "跳过复杂度判断与子问题分解")
    query = state["question"]  # 初始状态的 question 已是补充后的完整定向查询。
    retrieved = retrieve_documents(
        query,
        top_k=RETRIEVAL_TOP_K,
        knowledge_filenames=state.get("knowledge_filenames"),
        runtime=state.get("retrieval_runtime"),
    )  # 直接检索，跳过复杂度和扇出。
    results = retrieved.get("docs", [])  # 提取最终证据块。
    retrieve_meta = retrieved.get("meta", {})  # 提取检索诊断数据。
    context = _format_docs(results)  # 格式化为评分节点需要的引用上下文。
    _emit(
        state,
        "🧱",
        "HITL 三级分块检索",
        (
            f"叶子层 L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}"
        ),
    )
    _emit(
        state,
        "🧩",
        "Auto-merging 合并",
        (
            f"启用: {bool(retrieve_meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(retrieve_meta.get('auto_merge_applied'))}，"
            f"替换片段: {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    _emit(state, "✅", f"HITL 针对性检索完成，找到 {len(results)} 个片段", f"模式: {retrieve_meta.get('retrieval_mode', 'hybrid')}")
    rag_trace = state.get("rag_trace") or {}  # 延续恢复开始时已经建立的 HITL trace。
    rag_trace.update({
        "tool_used": True,  # 明确本阶段使用了检索工具。
        "tool_name": "search_knowledge_base",  # 记录调用的检索工具。
        "query": query,  # 保存定向检索使用的组合问题。
        "retrieved_chunks": results,  # 当前可用于重新评分的证据块。
        "hitl_targeted_retrieved_chunks": results,  # 单独保存恢复阶段召回快照。
        "hitl_resumed": True,  # 再次标记这属于恢复流程。
        "hitl_resume_strategy": "targeted_retrieval",  # 说明恢复策略为定向检索而非重跑主图。
        "retrieval_stage": "hitl_targeted_retrieval",  # 记录具体流程阶段。
        **retrieval_trace_fields(retrieve_meta),  # 展开本次底层检索的白名单诊断信息。
    })
    state.update({  # 就地合并定向检索结果，供下一行评分节点读取。
        "query": query,
        "docs": results,
        "context": context,
        "rag_trace": rag_trace,
    })
    state.update(grade_documents_node(state))  # 对新证据评分并写入恢复后的最终路由。
    return state


# 恢复入口直接运行恢复子图；若证据仍不足，可以生成新的最小 HITL 状态。
def resume_rag_from_hitl(
    resume_state: dict,
    user_answer: str,
    ctx: ChatRequestContext,
) -> dict:
    """从 HITL 断点恢复一次 RAG 运行，不重新进入首次运行的主图。

    如果针对性检索后仍需要用户澄清，会重新生成最小 ``hitl_resume_state``，使下一轮
    请求可以继续恢复，而无需保存不可序列化的图对象或模型连接。
    """
    state = _state_from_resume(resume_state, user_answer, ctx)  # 重建定向检索所需的干净状态。
    _emit(state, "▶️", "收到 HITL 补充，继续原 RAG 流程", user_answer)

    state = _retrieve_resume_query(state)  # 执行一次针对用户补充的检索和评分。
    if _is_hitl_result(state):  # 若仍需用户澄清，则生成下一轮可用的新快照。
        state["hitl_resume_state"] = _build_hitl_resume_state(state)
    return state


# 公开首次运行入口只接收问题和请求 Context，并把编译图的最终状态返回调用方。
def run_rag_graph(
    question: str,
    ctx: ChatRequestContext,
    *,
    retrieval_runtime: RetrievalRuntime | None = None,
) -> dict:
    """运行一次首次 RAG 图，并在命中 HITL 路由时附加可持久化恢复快照。"""
    result = rag_graph.invoke(
        _initial_state(question, ctx, retrieval_runtime=retrieval_runtime)
    )  # 用完整初始状态同步运行已编译主图。
    if _is_hitl_result(result):  # 首轮若暂停，也必须提供可持久化恢复状态。
        result["hitl_resume_state"] = _build_hitl_resume_state(result)
    return result
