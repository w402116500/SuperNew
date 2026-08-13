"""单次聊天请求共享的运行时上下文。

一个请求中的 Agent 工具、RAG 节点和 SSE 流式响应需要共享少量状态，例如检索诊断、
知识库工具调用额度和前端进度事件。本模块将这些状态绑定到 ``ChatRequestContext``，
避免并发请求通过模块全局变量互相污染。
"""

# 允许类方法的返回注解直接写 ChatRequestContext，而不必把类名写成字符串。
from __future__ import annotations

# asyncio 负责把 RAG 进度发送到流式响应队列。
import asyncio
# logging 记录后台线程投递事件失败的原因，不能让日志错误中断聊天主流程。
import logging
# RAG 可能在工作线程执行；RLock 保护同一请求内的共享字段。
import threading
# monotonic 不受系统时钟校准影响，适合计算请求和步骤耗时。
import time
# dataclass 自动生成初始化方法；field 用于提供独立锁和开始时间等默认值。
from dataclasses import dataclass, field
# Optional 表示流式请求才会具备的队列、事件循环或暂存 trace 可以为空。
from typing import Optional

# 规范化 trace 并校验 HITL 恢复状态，确保 Context 中只保留可序列化的安全数据。
from backend.schemas.chat import HitlResumeState, normalize_rag_trace

# 使用当前模块名创建日志记录器，便于日志定位到 request_context 模块。
logger = logging.getLogger(__name__)


# 每次聊天请求创建独立 Context，trace、工具预算和事件队列都不能放在模块全局变量。
@dataclass
class ChatRequestContext:
    """一个聊天请求显式传递给 Agent 工具和 RAG 节点的共享状态。

    Attributes:
        user_id: 当前请求所属用户，用于保证请求状态与用户身份对应。
        session_id: 当前会话 ID，用于保存消息和恢复 HITL 状态。
        knowledge_filenames: 本次检索允许使用的文件名范围；空元组表示不限制范围。
        output_queue: 流式接口的 asyncio 队列；同步接口不需要，因此可为 None。
        loop: 创建队列的事件循环；工作线程通过它安全地投递 SSE 进度事件。
    """

    # 两个 ID 在 Context 生命周期内不应变化，供所有下游工具读取。
    user_id: str
    session_id: str
    knowledge_filenames: tuple[str, ...] = ()
    # 流式请求才传入队列和事件循环；for_sync() 创建的 Context 不会发送步骤事件。
    output_queue: Optional[asyncio.Queue] = None
    loop: Optional[asyncio.AbstractEventLoop] = None

    # RLock 允许同一线程中的嵌套调用继续取得锁，并防止后台线程与请求线程竞争状态。
    _lock: threading.RLock = field(default_factory=threading.RLock)
    # close() 后设为 False，后续异步任务即使仍在运行也不能再向已关闭响应写数据。
    _active: bool = True
    # 暂存本轮 RAG trace；take_rag_trace() 读取后会清空它。
    _rag_trace: Optional[dict] = None
    # 记录本轮请求已成功取得的知识库工具槽位数量。
    _knowledge_tool_slots_used: int = 0
    # 使用单调时钟记录请求起点和上一步时间，以计算稳定的耗时指标。
    _started_at: float = field(default_factory=time.monotonic)
    _last_step_at: Optional[float] = None

    @classmethod
    def for_stream(
        cls,
        *,
        user_id: str,
        session_id: str,
        output_queue: asyncio.Queue,
        knowledge_filenames: tuple[str, ...] = (),
    ) -> ChatRequestContext:
        """创建支持流式进度事件的 Context，并绑定当前正在运行的事件循环。

        必须在 async 路由或协程中调用，因为 ``get_running_loop()`` 只能取得当前实际
        运行中的 asyncio 事件循环。后续工作线程会用这个 loop 安全写入 output_queue。
        """
        return cls(
            user_id=user_id,
            session_id=session_id,
            knowledge_filenames=knowledge_filenames,
            output_queue=output_queue,
            loop=asyncio.get_running_loop(),
        )

    @classmethod
    def for_sync(
        cls,
        *,
        user_id: str,
        session_id: str,
        knowledge_filenames: tuple[str, ...] = (),
    ) -> ChatRequestContext:
        """创建同步聊天使用的 Context，不绑定 SSE 队列或事件循环。"""
        return cls(
            user_id=user_id,
            session_id=session_id,
            knowledge_filenames=knowledge_filenames,
        )

    # RAG 可能在线程池运行，因此通过 loop.call_soon_threadsafe 把步骤安全送回 asyncio 队列。
    def emit_rag_step(
        self,
        icon: str,
        label: str,
        detail: str = "",
        *,
        group: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> None:
        """向流式响应队列发送一个 RAG 处理步骤及其耗时。

        该方法既可能由 FastAPI 协程调用，也可能由线程池中的 RAG 代码调用。因此只在
        锁内读取/更新共享状态，离开锁后再使用 ``call_soon_threadsafe`` 投递事件，避免
        队列操作阻塞其他 Context 操作。

        Args:
            icon: 前端显示的步骤图标标识。
            label: 简短步骤名称，例如“正在检索”。
            detail: 可选的补充说明。
            group: 可选的步骤分组标识。
            group_label: 可选的分组显示名称。
        """
        with self._lock:
            # 已关闭、同步请求或没有成功绑定事件循环时都不需要发送事件。
            if not self._active:
                return
            if self.output_queue is None or self.loop is None:
                return
            now = time.monotonic()
            last_step_at = self._last_step_at or self._started_at
            elapsed_ms = max(int((now - self._started_at) * 1000), 0)
            stage_elapsed_ms = max(int((now - last_step_at) * 1000), 0)
            self._last_step_at = now
            queue = self.output_queue
            loop = self.loop

        # 组装普通字典，保证它能被 SSE/JSON 序列化。
        step = {
            "icon": icon,
            "label": label,
            "detail": detail,
            "elapsed_ms": elapsed_ms,
            "stage_elapsed_ms": stage_elapsed_ms,
        }
        if group:
            step["group"] = group
        if group_label:
            step["group_label"] = group_label

        try:
            # 事件循环可能在请求结束时已经关闭，检查后再安排非线程安全的 put_nowait。
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "rag_step", "step": step},
                )
        except Exception:
            logger.exception("Failed to emit RAG step")

    # trace 和恢复状态在进入 Context 前再次规范化，工具只能保存可序列化数据。
    def store_rag_trace(self, rag_trace: dict, hitl_resume_state: Optional[dict] = None) -> None:
        """校验并暂存本轮 RAG trace；可选地一并保存 HITL 恢复状态。

        ``normalize_rag_trace`` 会删除未知字段并校验引用块结构；``HitlResumeState`` 再
        校验跨请求恢复图所需的最小状态，防止模型临时对象进入消息持久化流程。
        """
        current_trace = normalize_rag_trace(rag_trace)
        if not current_trace:
            # 空 trace 没有可保存的信息，也不覆盖本轮已有的有效 trace。
            return
        with self._lock:
            if self._active:
                self._rag_trace = {"rag_trace": current_trace}
                if hitl_resume_state:
                    # 先转成 Schema 再 model_dump，确保保存的是普通可序列化字典。
                    self._rag_trace["hitl_resume_state"] = HitlResumeState.model_validate(
                        hitl_resume_state
                    ).model_dump()

    # take 会读取后立即清空，防止下一轮消息误用上一轮 trace。
    def take_rag_trace(self) -> Optional[dict]:
        """取走本轮 trace 并立即清空，供消息存储等一次性消费者使用。"""
        with self._lock:
            context = self._rag_trace
            self._rag_trace = None
            return context

    def peek_rag_trace(self) -> Optional[dict]:
        """查看当前 trace 但不清空，适用于仍需继续处理同一轮请求的调用方。"""
        with self._lock:
            return self._rag_trace

    def reset_knowledge_tool_budget(self) -> None:
        """将知识库工具额度恢复为零，通常在开始一轮新的 Agent 流程时调用。"""
        with self._lock:
            self._knowledge_tool_slots_used = 0

    # 一次请求最多成功获取一个知识工具槽位，用代码约束代替只靠提示词自觉停止。
    def acquire_knowledge_tool_slot(self) -> bool:
        """尝试获取一次知识库工具调用额度。

        Returns:
            bool: 成功获取返回 True；本轮已调用过知识工具则返回 False。
        """
        with self._lock:
            if self._knowledge_tool_slots_used >= 1:
                return False
            self._knowledge_tool_slots_used += 1
            return True

    def close(self) -> None:
        """关闭 Context 并解除对流式队列和事件循环的引用。

        这不会停止已经运行的后台任务，但它们随后调用 ``emit_rag_step`` 时会因
        ``_active`` 为 False 而直接返回，避免向结束的 HTTP/SSE 响应继续写事件。
        """
        with self._lock:
            self._active = False
            self.output_queue = None
            self.loop = None
