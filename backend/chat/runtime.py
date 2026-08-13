"""聊天 Agent 的运行时装配：模型客户端、系统提示词和请求级工具绑定。"""

# os 用于从 .env 或系统环境中读取模型服务配置。
import os

# create_agent 把模型、工具和系统提示词组合为可执行 Agent。
from langchain.agents import create_agent
# init_chat_model 创建 OpenAI 兼容聊天模型客户端。
from langchain.chat_models import init_chat_model

# 请求 Context 提供单次请求的 SSE、trace 和知识工具调用额度。
from backend.chat.request_context import ChatRequestContext
# 天气工具是无状态共享工具；知识库工具必须为每个请求重新创建。
from backend.tools import get_current_weather, make_search_knowledge_base

API_KEY = os.getenv("ARK_API_KEY")  # 调用模型服务所需的认证密钥。
MODEL = os.getenv("MODEL")  # 主模型名称，负责工具决策和最终自然语言回答。
FAST_MODEL = os.getenv("FAST_MODEL")  # 辅助模型名称，负责较轻、较快的会话相关任务。
BASE_URL = os.getenv("BASE_URL")  # OpenAI 兼容模型服务的基础地址。

# 系统提示词是 Agent 的最高层行为约束：何时用工具、如何处理 RAG 状态、怎样引用证据。
SYSTEM_PROMPT = (
    # 角色与工具使用的基础约束。
    "You are AI智能知识检索系统, a reliable customer-service assistant that gives evidence-based answers from the store knowledge base. "
    "When responding, you may use tools to assist. "
    "Use search_knowledge_base when users ask document/knowledge questions. "
    # 工具调用预算由提示词提醒，并由 ChatRequestContext 中的代码硬限制兜底。
    "Do not call the same tool repeatedly in one turn. At most one knowledge tool call per turn. "
    "Once you call search_knowledge_base and receive its result, you MUST immediately produce the Final Answer based on that result. "
    "After receiving search_knowledge_base result, you MUST NOT call any tool again (including get_current_weather or search_knowledge_base). "
    # HITL 状态要求 Agent 追问或让用户选择，不能拿不充分的片段直接回答。
    "If the tool result starts with NEEDS_CLARIFICATION or NEEDS_SCOPE_SELECTION, ask the user the requested question directly and do not answer from retrieved context. "
    "If the tool result starts with NO_KNOWLEDGE, say the knowledge base does not contain reliable relevant information. "
    "If the retrieved context is insufficient, answer honestly that you don't know instead of making up facts. "
    # 检索工具会返回 [1]、[2] 编号片段；最终回答必须使用相同编号引用证据。
    "When answering based on retrieved chunks, you MUST cite the source chunks using their index numbers inline, for example [1] or [2][3]. "
    "Step-back questions and HyDE documents are retrieval aids only, not factual evidence. "
    "Base every factual claim only on retrieved source chunks. Answer only the question asked and omit unsupported extras, recommendations, mechanisms, defaults, or comparisons. "
    "Do not infer an operating mechanism or state from a listed option or value: for example, '60Hz or 120Hz' does not establish adaptive switching, and a listed 120Hz rate does not establish that it is fixed or the only supported mode. "
    "Do not reveal chain-of-thought. "
    "If you don't know the answer, admit it honestly."
)


# 主模型全局复用连接配置，负责 Agent 推理和最终回答。
model = init_chat_model(
    model=MODEL,  # 使用环境变量指定的主模型。
    model_provider="openai",  # 按 OpenAI 兼容协议请求模型服务。
    api_key=API_KEY,  # 传入认证密钥，不应写入源码或日志。
    base_url=BASE_URL,  # 支持官方地址或第三方/自建兼容网关。
    temperature=0.3,  # 保持回答相对稳定，同时保留少量自然表达变化。
    stream_usage=True,  # 收集流式调用用量；不等同于自动向 HTTP 客户端推送文本。
)

# 低延迟模型只维护会话笔记等辅助任务，不替代证据评分模型。
fast_model = init_chat_model(
    model=FAST_MODEL,  # 使用更低延迟的辅助模型。
    model_provider="openai",  # 与主模型使用同一种 API 协议。
    api_key=API_KEY,  # 复用同一认证密钥。
    base_url=BASE_URL,  # 复用同一模型服务地址。
    temperature=0.2,  # 辅助任务更偏确定性，因此温度略低于主模型。
    stream_usage=True,  # 统一采集模型调用用量。
)


# 每次请求重新绑定知识工具，使闭包拿到当前请求自己的 Context 和调用预算。
def create_agent_for_request(ctx: ChatRequestContext):
    """为一次聊天请求创建绑定当前 Context 的 Agent。

    Args:
        ctx: 当前请求独有的上下文，包含知识检索额度、SSE 步骤事件和 RAG trace。

    Returns:
        创建后的 LangChain Agent；其知识库工具闭包只能访问传入的这一个 ctx。
    """
    return create_agent(
        model=model,  # 所有请求复用同一个主模型客户端配置。
        tools=[
            get_current_weather,  # 无状态天气查询工具，可直接复用。
            make_search_knowledge_base(ctx),  # 请求级工具，携带本请求的预算与 trace。
        ],
        system_prompt=SYSTEM_PROMPT,  # 每次创建 Agent 都应用同一套安全和引用约束。
    )
