"""供聊天 Agent 调用的知识库检索工具工厂。"""

# @tool 将普通 Python 函数注册为 LangChain Agent 可调用的工具。
from langchain_core.tools import tool

# Context 绑定当前请求的工具预算、SSE 步骤事件和最终 RAG trace。
from backend.chat.request_context import ChatRequestContext


def make_search_knowledge_base(ctx: ChatRequestContext):
    """为一次聊天请求创建专属的 ``search_knowledge_base`` 工具。

    工具闭包捕获 ``ctx``，因此同一个请求中的多次工具调用共享调用额度和 trace；不同请求
    则创建各自独立的工具实例，状态不会串到其他用户或会话。
    """
    # 工具名会提供给 Agent 的 function calling 机制，模型通过该名字选择调用本函数。
    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """使用 RAG 流程检索知识库，并把证据或控制信号返回给 Agent。

        Args:
            query: Agent 根据用户问题传入的检索查询。

        Returns:
            str: 带编号的检索片段，或 ``NO_KNOWLEDGE``、``NEEDS_CLARIFICATION`` 等
                供 Agent 分支处理的状态前缀。
        """
        # 第二次工具调用会被代码拒绝，防止 Agent 在一次请求里反复检索。
        if not ctx.acquire_knowledge_tool_slot():
            return (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
                "Use the existing retrieval result and provide the final answer directly."
            )

        # Delayed import keeps tests and lightweight imports away from RAG/embedding startup.
        # 延迟导入避免普通启动和轻量测试提前加载 Embedding/RAG 重依赖。
        from backend.rag.pipeline import run_rag_graph  # 真正调用工具时才加载 RAG 图及其重依赖。

        rag_result = run_rag_graph(query, ctx)  # 运行完整 RAG 图：检索、评分、改写或子问题合成。

        docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []  # 最终可作为回答依据的证据块。
        rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}  # 本次 RAG 的结构化诊断数据。
        hitl_resume_state = (
            rag_result.get("hitl_resume_state")
            if isinstance(rag_result, dict)
            else None
        )
        # 工具结果正文交给 Agent，同时把结构化 trace 和恢复状态留在请求 Context。
        ctx.store_rag_trace(rag_trace, hitl_resume_state)  # Agent 只接收字符串，trace 留给请求结束时持久化。

        status = rag_trace.get("retrieval_status") if isinstance(rag_trace, dict) else None  # API 层稳定的检索状态。
        route = rag_trace.get("route") if isinstance(rag_trace, dict) else None  # RAG 图建议 Agent 采取的下一步路由。
        if status == "needs_clarification" or route == "clarify":
            prompt = rag_trace.get("hitl_prompt") or "I found related knowledge, but need one more detail before answering."  # 优先使用 RAG 生成的中文/业务追问。
            return f"NEEDS_CLARIFICATION: {prompt}"  # Agent 应向用户提问，不能直接编造答案。

        if status == "needs_scope_selection" or route == "scope_select":
            prompt = rag_trace.get("hitl_prompt") or "I found multiple related knowledge-base directions. Ask the user to choose one."  # 没有提示时使用英文保底文案。
            options = rag_trace.get("hitl_options") or []  # 读取可供用户选择的知识范围。
            if options:
                prompt = f"{prompt}\nOptions: " + "; ".join(str(item) for item in options)  # 将选项拼进 Agent 可读文本。
            return f"NEEDS_SCOPE_SELECTION: {prompt}"  # Agent 应请用户选择范围后再继续。

        if status == "no_knowledge" or route == "no_knowledge":
            return "NO_KNOWLEDGE: No reliable relevant documents were found in the knowledge base."  # 明确要求 Agent 不把猜测伪装成知识库答案。

        if not docs:
            return "No relevant documents found in the knowledge base."  # 兼容 trace 未给 no_knowledge 但最终文档为空的情况。

        formatted = []  # 收集按引用编号展示的证据文本。
        # 编号从 1 开始且保持检索顺序，模型回答中的 [1]、[2] 才能对应真实片段。
        for i, result in enumerate(docs, 1):
            source = result.get("filename", "Unknown")  # 文档来源文件名，缺失时显示 Unknown。
            page = result.get("page_number", "N/A")  # PDF 页码或 HTML 章节号，缺失时显示 N/A。
            text = result.get("text", "")  # 被召回的正文片段。
            formatted.append(f"[{i}] {source} (Page {page}):\n{text}")  # 编号与检索顺序保持一致。

        return "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)  # Agent 可据此在最终回答中引用 [1]、[2]。

    return search_knowledge_base  # 返回已注册且绑定当前请求 Context 的 LangChain 工具对象。
