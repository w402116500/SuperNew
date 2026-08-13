"""聊天服务编排：同步/流式回答、RAG、HITL 恢复与会话记忆持久化。"""

# asyncio 处理 SSE 流式任务、队列与线程池切换；json 用于编码 SSE data 事件。
import asyncio
import json
# datetime/uuid4 分别生成 HITL 暂停时间和唯一记录 ID。
from datetime import datetime, timezone
from uuid import uuid4

# LangChain 消息类型分别表示 AI、流式片段、用户和系统消息。
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

# Context 维护单次请求的 SSE 队列、RAG trace 和工具调用预算。
from backend.chat.request_context import ChatRequestContext
# runtime 创建请求级 Agent；fast_model 维护长期笔记；model 生成恢复后的回答。
from backend.chat.runtime import create_agent_for_request, fast_model, model
# storage 管理 PostgreSQL 消息/元数据与 Redis 缓存。
from backend.chat.storage import storage
# PendingHitlState 校验暂停快照；normalize_rag_trace 过滤不受控检索字段。
from backend.schemas.chat import PendingHitlState, normalize_rag_trace

CONTEXT_WINDOW_MESSAGES = 6  # 主模型每轮最多直接看到最近六条历史消息。
PENDING_HITL_KEY = "pending_hitl"  # 会话 metadata 中保存 HITL 暂停快照的键名。
HITL_STATUSES = {"needs_clarification", "needs_scope_selection"}  # 表示 RAG 暂停等待用户的状态集合。
HITL_ROUTES = {"clarify", "scope_select"}  # 与上述状态对应的图路由集合。


def _is_hitl_trace(rag_trace: dict | None) -> bool:
    """判断一份 RAG trace 是否表示本轮需要暂停并向用户追问。"""
    if not isinstance(rag_trace, dict):
        return False
    status = rag_trace.get("retrieval_status")  # 检索管线给出的标准结果状态。
    route = rag_trace.get("route")  # RAG 图最后选择的处理分支。
    return status in HITL_STATUSES or route in HITL_ROUTES


def _hitl_route_from_trace(rag_trace: dict) -> str:
    """把 trace 中的状态/路由统一归并为 clarify 或 scope_select。"""
    status = rag_trace.get("retrieval_status")  # 状态优先用于兼容不同版本的 trace。
    route = rag_trace.get("route")  # 路由作为状态缺失或不一致时的兜底依据。
    if status == "needs_scope_selection" or route == "scope_select":
        return "scope_select"
    return "clarify"


def _hitl_prompt_from_trace(rag_trace: dict) -> str:
    """获取 RAG 提供的 HITL 提示；缺失时按路由生成保守默认文案。"""
    prompt = (rag_trace.get("hitl_prompt") or "").strip()  # 管线可为当前问题定制追问文案。
    if prompt:
        return prompt
    route = _hitl_route_from_trace(rag_trace)
    if route == "scope_select":
        return "我找到了多个可能相关的知识库方向，请选择你想继续查询的方向。"
    return "我找到了相关知识，但还缺少一个关键信息，请补充后我继续查询。"


def _hitl_options_from_trace(rag_trace: dict) -> list[str]:
    """提取并清理范围选择候选项，保证返回非空字符串列表。"""
    options = rag_trace.get("hitl_options") or []  # scope_select 路由可能携带候选知识方向。
    if not isinstance(options, list):
        return []
    return [str(option).strip() for option in options if str(option).strip()]


def _format_hitl_message(prompt: str, options: list[str] | None = None) -> str:
    """将 HITL 提问和可选项格式化为最终展示给用户的纯文本。"""
    clean_prompt = prompt.strip()  # 去掉模型文案首尾的无意义空白。
    clean_options = [item for item in (options or []) if item]  # 避免渲染空白选项。
    if not clean_options:
        return clean_prompt
    option_lines = "\n".join(f"- {item}" for item in clean_options)
    return f"{clean_prompt}\n\n可选方向：\n{option_lines}"


def _existing_hitl_answers(pending_hitl: dict | None) -> list[str]:
    """读取历史 HITL 补充答案，并过滤无效或空白项。"""
    if not isinstance(pending_hitl, dict):
        return []
    answers = pending_hitl.get("answers") or []  # 多次追问时累计的用户补充。
    if not isinstance(answers, list):
        return []
    return [str(answer).strip() for answer in answers if str(answer).strip()]


def _build_pending_hitl(
    rag_trace: dict,
    original_question: str,
    previous_answers: list[str] | None = None,
    resume_state: dict | None = None,
) -> dict:
    """将本轮暂停 trace 转换为可存入会话 metadata 的 PendingHitlState 快照。"""
    prompt = _hitl_prompt_from_trace(rag_trace)  # 可直接展示给用户的澄清问题。
    options = _hitl_options_from_trace(rag_trace)  # 可选范围列表；普通澄清时为空。
    route = _hitl_route_from_trace(rag_trace)  # 统一后的下一步恢复类型。
    return PendingHitlState(
        id=uuid4().hex,
        original_question=original_question,
        prompt=prompt,
        options=options,
        route=route,
        retrieval_status=(
            "needs_scope_selection" if route == "scope_select" else "needs_clarification"
        ),
        answers=previous_answers or [],
        resume_state=resume_state,
        created_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump()


def _build_hitl_event(pending_hitl: dict) -> dict:
    """从完整暂停快照提取可通过 SSE 发送给前端的安全字段。"""
    return {
        "id": pending_hitl["id"],
        "prompt": pending_hitl["prompt"],
        "options": pending_hitl["options"],
        "route": pending_hitl["route"],
        "retrieval_status": pending_hitl["retrieval_status"],
        "original_question": pending_hitl["original_question"],
    }


def _build_hitl_resume_query(pending_hitl: dict, user_text: str) -> str:
    """把原问题、已有补充与本轮补充合成为 Agent 可理解的继续请求。"""
    original_question = pending_hitl.get("original_question") or ""  # 首次触发 HITL 的完整问题。
    prompt = pending_hitl.get("prompt") or ""  # 上轮系统要求补充的具体信息。
    previous_answers = _existing_hitl_answers(pending_hitl)  # 可能已有的多轮补充内容。

    lines = [
        "这是上一轮 RAG 流程中 HITL 澄清后的继续请求。",
        "不要把用户补充单独当成新问题；请回到原始问题继续完成回答。",
        f"原始问题：{original_question}",
    ]
    if prompt:
        lines.append(f"HITL 问题：{prompt}")
    if previous_answers:
        lines.append("此前用户已补充：")
        lines.extend(f"- {answer}" for answer in previous_answers)
    lines.extend([
        f"本轮用户补充：{user_text}",
        "请基于以上补充形成完整查询，并按原来的 Agent/RAG 流程继续。",
    ])
    return "\n".join(lines)


def _current_pending_hitl(value: dict | None) -> dict | None:
    """校验 metadata 中的暂停快照；旧格式或损坏数据按不存在处理。"""
    if not isinstance(value, dict):
        return None
    try:
        return PendingHitlState.model_validate(value).model_dump()
    except ValueError:
        return None


def _pending_resume_state(pending_hitl: dict | None) -> dict | None:
    """从暂停快照中复制出 RAG 恢复所需的最小状态字典。"""
    if not isinstance(pending_hitl, dict):
        return None
    resume_state = pending_hitl.get("resume_state")  # 由 RAG 图保存的中断现场。
    return dict(resume_state) if isinstance(resume_state, dict) else None


def _extract_ai_content(msg) -> str:
    """兼容字符串、内容块列表和普通对象，统一提取模型输出的可见文本。"""
    content = getattr(msg, "content", "")  # LangChain 不同消息类型的正文形状并不完全相同。
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = ""
        for block in content:
            if isinstance(block, str):
                text += block
            elif isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        return text
    return str(content or "")


def _format_retrieved_chunks(docs: list[dict]) -> str:
    """将恢复检索到的文档块格式化为带 [1]、[2] 引用编号的上下文。"""
    formatted = []  # 每个元素对应一个可被模型引用的检索证据块。
    for i, result in enumerate(docs, 1):
        source = result.get("filename", "Unknown")
        page = result.get("page_number", "N/A")
        text = result.get("text", "")
        formatted.append(f"[{i}] {source} (Page {page}):\n{text}")
    return "\n\n---\n\n".join(formatted)


def _build_resume_answer_messages(
    pending_hitl: dict,
    user_answer: str,
    docs: list[dict],
) -> list:
    """构造 HITL 恢复后调用主模型回答原问题所需的 system/human 消息。"""
    original_question = pending_hitl.get("original_question") or ""  # 回答目标始终是原问题。
    prompt = pending_hitl.get("prompt") or ""  # 补充问题也提供给回答模型理解上下文。
    context = _format_retrieved_chunks(docs)  # 带编号的证据正文，编号即引用号。
    system = SystemMessage(
        content=(
            "You are a helpful knowledge-base assistant. "
            "Answer the user's original question using only the retrieved chunks. "
            "You MUST cite source chunks inline with [1], [2], etc. "
            "If the chunks are insufficient, say so honestly. "
            "Do not mention internal HITL or RAG implementation details."
        )
    )
    human = HumanMessage(
        content=(
            "原始问题：\n"
            f"{original_question}\n\n"
            "HITL 补充问题：\n"
            f"{prompt}\n\n"
            "用户补充：\n"
            f"{user_answer}\n\n"
            "检索片段：\n"
            f"{context}\n\n"
            "请基于检索片段回答原始问题，并使用 [1]、[2] 这样的引用。"
        )
    )
    return [system, human]


def _no_knowledge_response() -> str:
    """返回统一的中文无知识库证据提示，避免模型凭记忆补全事实。"""
    return "知识库中没有找到可靠的相关信息，暂时无法基于知识库回答这个问题。"


def _resume_rag_from_hitl_sync(pending_hitl: dict, user_answer: str, ctx: ChatRequestContext) -> dict:
    """在同步线程中从已保存 HITL 状态恢复 RAG；没有恢复状态时返回空字典。"""
    from backend.rag.pipeline import resume_rag_from_hitl

    resume_state = _pending_resume_state(pending_hitl)
    if not resume_state:
        return {}
    return resume_rag_from_hitl(resume_state, user_answer, ctx)


def _answer_resumed_rag_sync(pending_hitl: dict, user_answer: str, rag_result: dict) -> str:
    """使用恢复后的证据块生成最终回答；无可靠文档时直接返回无知识提示。"""
    docs = rag_result.get("docs") or []  # 恢复检索最终保留的证据块。
    trace = rag_result.get("rag_trace") or {}  # 兼容状态仍只出现在 trace 的实现。
    status = rag_result.get("retrieval_status") or trace.get("retrieval_status")  # 优先读取顶层标准状态。
    route = rag_result.get("route") or trace.get("route")  # 路由同样兼容顶层或 trace 两种位置。
    if status == "no_knowledge" or route == "no_knowledge" or not docs:
        return _no_knowledge_response()
    res = model.invoke(_build_resume_answer_messages(pending_hitl, user_answer, docs))
    return _extract_ai_content(res)


def _build_context_messages(
    messages: list,
    persistent_note: str,
    user_text: str,
) -> list:
    """组合长期笔记、最近历史与当前输入，生成主 Agent 的有限上下文窗口。"""
    short_term = messages[-CONTEXT_WINDOW_MESSAGES:] if len(messages) > CONTEXT_WINDOW_MESSAGES else messages  # 超出窗口时只保留最近六条真实消息。
    context_messages: list = []  # 最终发送给 Agent 的 LangChain 消息列表。
    if persistent_note:
        context_messages.append(
            SystemMessage(
                content=(
                    "【对话持久化笔记（你的工作记忆）】\n"
                    f"{persistent_note}\n"
                    "请参考以上笔记保持对话连贯性，避免重复回答已解决的问题。"
                )
            )
        )
    context_messages.extend(short_term)  # 保持历史消息的原有顺序。
    context_messages.append(HumanMessage(content=user_text))  # 将本轮用户输入放在最后。
    return context_messages


def _should_update_persistent_note(messages: list, current_note: str) -> bool:
    """仅在已有笔记或短期窗口开始截断历史时，才支付一次笔记维护模型调用。"""
    return bool(current_note) or len(messages) > CONTEXT_WINDOW_MESSAGES


async def update_persistent_note(
    current_note: str,
    user_text: str,
    ai_response: str,
    history_messages: list | None = None,
) -> str:
    """在线程池更新持久化笔记，避免同步模型调用阻塞 SSE 事件循环。"""
    loop = asyncio.get_running_loop()  # 获取当前异步事件循环。
    return await loop.run_in_executor(
        None,
        lambda: _update_persistent_note_sync(
            current_note,
            user_text,
            ai_response,
            history_messages=history_messages,
        ),
    )


def generate_session_title(user_text: str) -> str:
    """用首条用户输入生成不超过 16 个字符的会话标题。"""
    compact_title = " ".join(user_text.split()).strip(" \t\r\n。！？!?，,；;：:")  # 压缩空白并去掉常见收尾标点。
    return compact_title[:16] or "新会话"  # 输入为空时提供稳定的默认标题。


def _update_persistent_note_sync(
    current_note: str,
    user_text: str,
    ai_response: str,
    *,
    history_messages: list | None = None,
) -> str:
    """调用辅助模型合并长期工作笔记；模型故障时保留旧笔记而不中断聊天。"""
    try:
        history_text = ""  # 仅首次建立笔记时附带完整旧历史，减少后续 token 消耗。
        if history_messages:
            history_lines = []
            for message in history_messages:
                role = "用户" if isinstance(message, HumanMessage) else "AI"
                history_lines.append(f"{role}：{_extract_ai_content(message)}")
            history_text = (
                "\n\n▼ 首次建立笔记时需要一并概括的此前对话：\n"
                + "\n".join(history_lines)
                + "\n\n"
            )
        prompt = (
            "你是一个【Context Manager Agent】(上下文管理器)，负责维护多轮对话中的「持久化笔记」。\n"
            "笔记是模型在有限上下文窗口下的长效工作记忆，记录已解决的问题与关键事实。\n\n"
            "更新规则：\n"
            "1. 将新信息与现有笔记智能合并，不要简单拼接。\n"
            "2. 过滤噪音，控制在 500 字以内，用简明条目输出。\n"
            "3. 若信息冲突，保留最可靠或最新版本。\n\n"
            f"▼ 现有笔记：\n{current_note if current_note else '无'}\n\n"
            f"{history_text}"
            f"▼ 最新一轮对话：\n用户：{user_text}\nAI：{ai_response}\n\n"
            "请直接输出更新后的笔记（纯文本，不要解释或 Markdown 代码块）："
        )
        res = fast_model.invoke([HumanMessage(content=prompt)])  # 使用低延迟辅助模型整理笔记。
        return (res.content or "").strip()  # 统一返回干净纯文本。
    except Exception as e:
        print(f"Context Manager Error: {e}")
        return current_note


def chat_with_agent(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
    knowledge_filenames: list[str] | None = None,
):
    """执行一次非流式聊天请求，并返回最终正文和规范化 RAG trace。

    该入口会处理普通 Agent 对话及 HITL 恢复两条路径，最后总会把用户消息、AI 消息和
    会话 metadata 作为完整快照写回 storage。
    """
    messages, metadata = storage.load_with_meta(user_id, session_id)  # 读取历史 LangChain 消息与会话 metadata。
    persistent_note = metadata.get("persistent_note", "")  # 长期笔记补充被截断的早期对话。
    is_first_message = len(messages) == 0  # 首条消息用于生成会话标题。
    stored_pending_hitl = metadata.get(PENDING_HITL_KEY)  # 从上轮 metadata 读取原始暂停快照。
    pending_hitl = _current_pending_hitl(stored_pending_hitl)  # Pydantic 校验后才允许用于恢复。
    invalid_pending_hitl = stored_pending_hitl is not None and pending_hitl is None  # 标记需要在本轮清理的旧/损坏快照。
    is_hitl_resume = isinstance(pending_hitl, dict)  # 有有效快照代表当前输入是对上一轮追问的回答。
    resume_state = _pending_resume_state(pending_hitl)  # RAG 图能否原地恢复取决于该状态是否存在。
    effective_user_text = (
        _build_hitl_resume_query(pending_hitl, user_text)
        if is_hitl_resume
        else user_text
    )
    hitl_answers = _existing_hitl_answers(pending_hitl)  # 为再次追问保留此前所有用户补充。
    if is_hitl_resume:
        hitl_answers = [*hitl_answers, user_text]
    original_question = (  # 新问题以本轮输入为原问题；恢复时沿用最初的问题。
        pending_hitl.get("original_question")
        if is_hitl_resume
        else user_text
    )

    ctx = ChatRequestContext.for_sync(
        user_id=user_id,
        session_id=session_id,
        knowledge_filenames=tuple(knowledge_filenames or ()),
    )  # 同步请求不绑定 SSE 队列。
    ctx.reset_knowledge_tool_budget()  # 每个新 HTTP 请求重新获得一次知识库工具额度。

    try:
        messages.append(HumanMessage(content=user_text))  # 先把本轮用户问题加入完整会话快照。
        storage.save(user_id, session_id, messages)  # 即使模型后续失败，也保留用户已发送的消息。

        if is_hitl_resume and resume_state:  # 已有有效暂停快照时，跳过普通 Agent，直接恢复 RAG。
            rag_result = _resume_rag_from_hitl_sync(pending_hitl, user_text, ctx)
            rag_trace = normalize_rag_trace(
                rag_result.get("rag_trace") if isinstance(rag_result, dict) else None
            )
            next_pending_hitl = None  # 只有 RAG 再次要求补充时才会生成新的暂停快照。
            if _is_hitl_trace(rag_trace):
                next_pending_hitl = _build_pending_hitl(
                    rag_trace,
                    original_question or user_text,
                    previous_answers=hitl_answers,
                    resume_state=rag_result.get("hitl_resume_state"),
                )
                response_content = _format_hitl_message(
                    next_pending_hitl["prompt"],
                    next_pending_hitl["options"],
                )
            else:
                response_content = _answer_resumed_rag_sync(pending_hitl, user_text, rag_result)
        else:  # 普通新问题走请求级 Agent 和工具调用流程。
            request_agent = create_agent_for_request(ctx)
            context_messages = _build_context_messages(messages[:-1], persistent_note, effective_user_text)
            result = request_agent.invoke(
                {"messages": context_messages},
                config={"recursion_limit": 8},
            )

            response_content = ""  # 兼容 Agent 返回字典、消息对象或普通字符串。
            if isinstance(result, dict):
                if "output" in result:
                    response_content = result["output"]
                elif "messages" in result and result["messages"]:
                    msg = result["messages"][-1]  # LangGraph 常把最终模型消息放在列表末尾。
                    response_content = getattr(msg, "content", str(msg))
                else:
                    response_content = str(result)
            elif hasattr(result, "content"):
                response_content = result.content
            else:
                response_content = str(result)

            stored_trace = ctx.take_rag_trace()  # 读取工具在本请求 Context 中写入的结果并清空它。
            rag_trace = normalize_rag_trace(stored_trace.get("rag_trace") if stored_trace else None)  # 只保留可对外/可持久化的诊断字段。
            resume_state_from_trace = stored_trace.get("hitl_resume_state") if stored_trace else None  # 用于下一轮从该节点继续图执行。
            next_pending_hitl = None
            if _is_hitl_trace(rag_trace):
                next_pending_hitl = _build_pending_hitl(
                    rag_trace,
                    original_question or user_text,
                    previous_answers=hitl_answers,
                    resume_state=resume_state_from_trace,
                )
                response_content = _format_hitl_message(
                    next_pending_hitl["prompt"],
                    next_pending_hitl["options"],
                )

        save_meta = dict(metadata)  # 复制旧 metadata，避免覆盖无关会话字段。
        if invalid_pending_hitl:  # 不让无法校验的旧状态永久阻塞后续会话。
            save_meta[PENDING_HITL_KEY] = None
        if is_first_message:
            save_meta["title"] = generate_session_title(user_text)
        if next_pending_hitl:  # 暂停时不更新笔记，等待问题真正得到回答后再总结。
            save_meta[PENDING_HITL_KEY] = next_pending_hitl
        else:
            if is_hitl_resume:
                save_meta[PENDING_HITL_KEY] = None
            if _should_update_persistent_note(messages, persistent_note):
                save_meta["persistent_note"] = _update_persistent_note_sync(
                    persistent_note,
                    effective_user_text,
                    response_content,
                    history_messages=messages[:-1] if not persistent_note else None,
                )

        messages.append(AIMessage(content=response_content))  # 将最终回答加入同一会话快照。
        extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]  # 只有最后一条 AI 消息保存本轮 trace。
        storage.save(
            user_id,
            session_id,
            messages,
            metadata=save_meta,
            extra_message_data=extra_message_data,
        )

        return {
            "response": response_content,  # 非流式 HTTP 接口展示的最终回答。
            "rag_trace": rag_trace,  # 前端可选展示的本轮检索诊断。
        }
    finally:
        ctx.close()  # 即使发生异常也关闭请求 Context，拒绝后台任务继续写状态。


async def chat_with_agent_stream(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
    knowledge_filenames: list[str] | None = None,
):
    """执行 SSE 流式聊天：持续 yield 进度、文本、trace、HITL 请求和完成标记。

    事件格式为 ``data: <JSON>\\n\\n``，浏览器 EventSource 可逐条接收。普通 Agent 路径
    通过异步任务读取 token；HITL 恢复路径将同步 RAG 放入线程池，并持续转发其进度事件。
    """
    initial_step = {  # 在任何模型/数据库操作前立即反馈请求已被服务端接收。
        "type": "rag_step",
        "step": {
            "icon": "📨",
            "label": "请求已接收，正在准备回答",
            "detail": "",
            "elapsed_ms": 0,
            "stage_elapsed_ms": 0,
        },
    }
    yield f"data: {json.dumps(initial_step)}\n\n"  # SSE 每个事件以两个换行结束。

    messages, metadata = storage.load_with_meta(user_id, session_id)
    persistent_note = metadata.get("persistent_note", "")
    is_first_message = len(messages) == 0
    stored_pending_hitl = metadata.get(PENDING_HITL_KEY)  # 读取上一次流式请求可能保存的暂停快照。
    pending_hitl = _current_pending_hitl(stored_pending_hitl)  # 校验快照结构，防止脏数据进入恢复逻辑。
    invalid_pending_hitl = stored_pending_hitl is not None and pending_hitl is None  # 本轮保存时应移除的无效快照标记。
    is_hitl_resume = isinstance(pending_hitl, dict)  # 决定走普通 Agent 还是恢复 RAG。
    resume_state = _pending_resume_state(pending_hitl)  # 保存的图状态存在时才可跳过重新检索。
    effective_user_text = (
        _build_hitl_resume_query(pending_hitl, user_text)
        if is_hitl_resume
        else user_text
    )
    hitl_answers = _existing_hitl_answers(pending_hitl)  # 后续 HITL 轮次会继续带上这些已有回答。
    if is_hitl_resume:
        hitl_answers = [*hitl_answers, user_text]
    original_question = (  # HITL 恢复过程中必须固定最初的问题，不能误用短补充文本。
        pending_hitl.get("original_question")
        if is_hitl_resume
        else user_text
    )

    output_queue = asyncio.Queue()  # 在 RAG 工作线程、Agent worker 与 SSE 生成器之间传递事件。
    ctx = ChatRequestContext.for_stream(
        user_id=user_id,
        session_id=session_id,
        output_queue=output_queue,
        knowledge_filenames=tuple(knowledge_filenames or ()),
    )
    ctx.reset_knowledge_tool_budget()  # 流式请求同样每轮只能成功调用一次知识库工具。

    try:
        messages.append(HumanMessage(content=user_text))
        storage.save(user_id, session_id, messages)

        if is_hitl_resume and resume_state:  # 恢复路径不运行普通 Agent，而是继续此前暂停的 RAG 图。
            loop = asyncio.get_running_loop()  # 取得事件循环，用于把同步 RAG 放到默认线程池。
            resume_future = loop.run_in_executor(
                None,
                lambda: _resume_rag_from_hitl_sync(pending_hitl, user_text, ctx),
            )

            while not resume_future.done():  # RAG 尚在工作时持续转发其写入 Context 队列的进度事件。
                try:
                    event = await asyncio.wait_for(output_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield f"data: {json.dumps(event)}\n\n"

            while not output_queue.empty():  # future 完成后清空可能在最后一刻入队的事件。
                event = output_queue.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"

            rag_result = await resume_future  # 取得线程池内恢复 RAG 的最终字典结果。
            rag_trace = normalize_rag_trace(
                rag_result.get("rag_trace") if isinstance(rag_result, dict) else None
            )
            next_pending_hitl = None  # 仅在恢复后仍需追问时赋值。
            full_response = ""  # 累积后续流式 token，最终也会写入消息存储。

            if _is_hitl_trace(rag_trace):  # 证据仍不够时创建下一轮暂停快照。
                next_pending_hitl = _build_pending_hitl(
                    rag_trace,
                    original_question or user_text,
                    previous_answers=hitl_answers,
                    resume_state=rag_result.get("hitl_resume_state"),
                )
                full_response = _format_hitl_message(
                    next_pending_hitl["prompt"],
                    next_pending_hitl["options"],
                )
            elif not (rag_result.get("docs") if isinstance(rag_result, dict) else None):  # 没有文档时不调用回答模型。
                full_response = _no_knowledge_response()
                yield f"data: {json.dumps({'type': 'content', 'content': full_response})}\n\n"
            else:  # 有证据时用主模型的 astream 逐 token 生成恢复后的最终答案。
                answer_messages = _build_resume_answer_messages(
                    pending_hitl,
                    user_text,
                    rag_result.get("docs") or [],
                )
                async for msg in model.astream(answer_messages):
                    content = _extract_ai_content(msg)
                    if content:
                        full_response += content
                        yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"

            save_meta = dict(metadata)  # 保留标题等本轮不修改的 metadata 字段。
            if invalid_pending_hitl:
                save_meta[PENDING_HITL_KEY] = None
            if next_pending_hitl:
                save_meta[PENDING_HITL_KEY] = next_pending_hitl
            else:
                save_meta[PENDING_HITL_KEY] = None
                if _should_update_persistent_note(messages, persistent_note):
                    try:
                        save_meta["persistent_note"] = await update_persistent_note(
                            persistent_note,
                            effective_user_text,
                            full_response,
                            history_messages=messages[:-1] if not persistent_note else None,
                        )
                    except Exception as e:
                        print(f"Update persistent note error: {e}")

            messages.append(AIMessage(content=full_response))
            extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
            storage.save(
                user_id,
                session_id,
                messages,
                metadata=save_meta,
                extra_message_data=extra_message_data,
            )

            if rag_trace:  # 将诊断作为独立 SSE 事件发送，避免混入用户可见正文。
                yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace})}\n\n"

            if next_pending_hitl:  # 前端收到后可展示澄清问题和选项。
                yield f"data: {json.dumps({'type': 'hitl_request', 'hitl': _build_hitl_event(next_pending_hitl)})}\n\n"

            yield "data: [DONE]\n\n"  # 与 OpenAI 风格一致的流结束标记。
            return

        request_agent = create_agent_for_request(ctx)  # 普通路径为当前请求创建绑定工具 Context 的 Agent。
        context_messages = _build_context_messages(messages[:-1], persistent_note, effective_user_text)

        session_title = None  # 非首轮保持原有标题，首轮才在 SSE 中下发新标题。
        if is_first_message:
            session_title = generate_session_title(user_text)
            yield f"data: {json.dumps({'type': 'session_title', 'title': session_title, 'session_id': session_id})}\n\n"

        full_response = ""  # 逐片段拼接，最终作为完整 AIMessage 保存。
        agent_error = None  # 后台任务异常后阻止继续发送 trace、完成标记和落库。

        async def _agent_worker():
            """在后台消费 Agent token 流，并将可见文本或错误写入 output_queue。"""
            nonlocal full_response, agent_error
            try:
                async for msg, _metadata in request_agent.astream(
                    {"messages": context_messages},
                    stream_mode="messages",
                    config={"recursion_limit": 8},
                ):
                    if not isinstance(msg, AIMessageChunk):  # 工具调用/状态消息不应直接作为正文推给前端。
                        continue
                    if getattr(msg, "tool_call_chunks", None):  # 跳过模型生成的工具调用参数片段。
                        continue

                    content = ""  # 兼容 LangChain 的字符串正文与多模态内容块格式。
                    if isinstance(msg.content, str):
                        content = msg.content
                    elif isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, str):
                                content += block
                            elif isinstance(block, dict) and block.get("type") == "text":
                                content += block.get("text", "")

                    if content:
                        stored_trace = ctx.peek_rag_trace()
                        rag_trace = normalize_rag_trace(
                            stored_trace.get("rag_trace") if stored_trace else None
                        )
                        if _is_hitl_trace(rag_trace):  # 工具已经要求 HITL 时，不再转发模型可能生成的普通回答。
                            continue
                        full_response += content
                        await output_queue.put({"type": "content", "content": content})
            except Exception as e:
                agent_error = str(e)
                await output_queue.put({"type": "error", "content": str(e)})
            finally:
                await output_queue.put(None)  # None 是通知外层 SSE 循环“Agent worker 已结束”的哨兵值。

        agent_task = asyncio.create_task(_agent_worker())  # 启动后台 token 消费，不阻塞外层 SSE 生成器。

        try:
            while True:
                event = await output_queue.get()
                if event is None:  # 收到哨兵值后停止读取本轮 Agent 事件。
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:  # 客户端断开 SSE 连接时取消后台任务，避免无主模型调用继续运行。
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if not agent_task.done():
                agent_task.cancel()

        if agent_error:  # 错误事件已由 worker 发出，不能把不完整回答当成成功结果保存。
            return

        stored_trace = ctx.take_rag_trace()  # 取走并清空本轮工具保存的 trace，防止下一轮误用。
        rag_trace = normalize_rag_trace(stored_trace.get("rag_trace") if stored_trace else None)
        resume_state_from_trace = stored_trace.get("hitl_resume_state") if stored_trace else None  # 下轮恢复 RAG 图的断点状态。
        next_pending_hitl = None  # 普通回答完成时保持 None，HITL 时改为完整快照。
        hitl_response_content = ""  # 先单独构建追问，再覆盖此前可能流出的普通文本。
        if _is_hitl_trace(rag_trace):  # 将 Agent 工具产生的 HITL 信号转换为可持久化暂停状态。
            next_pending_hitl = _build_pending_hitl(
                rag_trace,
                original_question or user_text,
                previous_answers=hitl_answers,
                resume_state=resume_state_from_trace,
            )
            hitl_response_content = _format_hitl_message(
                next_pending_hitl["prompt"],
                next_pending_hitl["options"],
            )

        save_meta = dict(metadata)  # 基于旧 metadata 增量更新，避免丢失其他会话配置。
        if invalid_pending_hitl:
            save_meta[PENDING_HITL_KEY] = None
        if session_title:
            save_meta["title"] = session_title

        if next_pending_hitl:  # 暂停时保存快照，并用追问文本替换任何已生成的普通回答。
            save_meta[PENDING_HITL_KEY] = next_pending_hitl
            full_response = hitl_response_content
        else:
            if is_hitl_resume and not agent_error:
                save_meta[PENDING_HITL_KEY] = None
            if _should_update_persistent_note(messages, persistent_note):
                try:
                    save_meta["persistent_note"] = await update_persistent_note(
                        persistent_note,
                        effective_user_text,
                        full_response,
                        history_messages=messages[:-1] if not persistent_note else None,
                    )
                except Exception as e:
                    print(f"Update persistent note error: {e}")

        messages.append(AIMessage(content=full_response))  # 将最终正文或 HITL 提问保存为一条 AI 消息。
        extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]  # RAG trace 只附着到最后 AI 消息。
        storage.save(
            user_id,
            session_id,
            messages,
            metadata=save_meta,
            extra_message_data=extra_message_data,
        )
        if rag_trace:
            yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace})}\n\n"

        if next_pending_hitl:
            yield f"data: {json.dumps({'type': 'hitl_request', 'hitl': _build_hitl_event(next_pending_hitl)})}\n\n"

        yield "data: [DONE]\n\n"
    except Exception as exc:  # 初始化、RAG、模型或存储失败时，以 SSE 错误事件通知前端。
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
    finally:
        ctx.close()  # 连接正常结束、异常或取消时都释放请求级 Context。
