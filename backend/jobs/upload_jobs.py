"""上传与删除任务的进度管理。

轻量版先使用进程内存保存任务状态，适合当前单进程开发部署。
如果后续要支持多进程或服务重启恢复，可以把同样的数据结构迁移到 Redis/PostgreSQL。
"""

# annotations 允许在类型标注中使用 dict[str, dict]、list[...] 等现代写法。
from __future__ import annotations

# deepcopy 用于返回独立任务快照，防止调用方修改管理器内部数据。
from copy import deepcopy
# UTC 和 datetime 用于生成统一的任务创建、更新时间。
from datetime import UTC, datetime
# Lock 用于保护多个后台线程同时读写任务字典。
from threading import Lock
# Literal 限制状态字段只能使用指定字符串值。
from typing import Literal
# uuid4 用于生成不易冲突的任务 ID。
from uuid import uuid4


# StepStatus 表示单个步骤可处于的四种状态。
StepStatus = Literal["pending", "running", "completed", "failed"]
# JobStatus 表示整个上传或删除任务可处于的四种状态。
JobStatus = Literal["pending", "running", "completed", "failed"]


# 上传任务默认步骤。每项是 (内部 key, 前端显示标签)。
DEFAULT_STEPS = [
    ("upload", "文档上传"),
    ("mineru", "MinerU 转 Markdown"),
    ("chunk", "Markdown 三级分块"),
    ("cleanup", "替换旧版本"),
    ("parent_store", "父级分块入库"),
    ("vector_store", "向量化入库"),
]

# 删除任务使用不同步骤，但可复用相同 UploadJobManager 状态机。
DELETE_STEPS = [
    ("prepare", "准备删除"),
    ("bm25", "同步 BM25 统计"),
    ("milvus", "删除向量数据"),
    ("parent_store", "删除父级分块"),
]


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    Returns:
        str: 例如 ``2026-07-18T14:30:00.123456+00:00`` 的统一时间戳。
    """
    # 使用 UTC 避免服务器本地时区导致任务时间难以比较。
    return datetime.now(UTC).isoformat()


class UploadJobManager:
    """线程安全的上传或删除任务状态容器。

    Attributes:
        _jobs: 以 job_id 为键保存任务字典的进程内存存储。
        _lock: 保护 _jobs 的互斥锁，避免后台线程和轮询请求产生竞争条件。
    """

    def __init__(self):
        """创建空任务字典和一把供所有任务操作共用的锁。"""
        # 字典结构为：{任务 ID: 任务状态字典}。
        self._jobs: dict[str, dict] = {}
        # 任务字典由多个后台线程共享，每次读写都必须使用同一把锁。
        self._lock = Lock()

    def create_job(
        self,
        filename: str,
        *,
        steps: list[tuple[str, str]] | None = None,
        current_step: str = "upload",
        message: str = "等待上传",
        completion_step: str = "vector_store",
    ) -> dict:
        """创建一个待执行任务，并返回不会影响内部状态的任务快照。

        Args:
            filename: 当前上传或删除操作关联的文件名。
            steps: 可选步骤表；不传时使用 DEFAULT_STEPS。
            current_step: 初始当前步骤的内部 key。
            message: 初始展示消息。
            completion_step: 成功完成时要写入 current_step 的最终步骤 key。

        Returns:
            dict: 新建任务的深拷贝快照，包含 job_id、状态、步骤和进度字段。
        """
        # 上传和删除可以复用状态机，只需传入不同步骤表和完成节点。
        steps = steps or DEFAULT_STEPS
        # uuid4().hex 生成 32 位十六进制字符串，适合作为 API 轮询使用的任务 ID。
        job_id = uuid4().hex
        # 同一次创建的 created_at、updated_at 使用同一个时间。
        now = _now_iso()
        job = {
            "job_id": job_id,
            "filename": filename,
            "status": "pending",
            "current_step": current_step,
            "message": message,
            # 完成节点用于区分上传和删除，避免 complete_job 写死最后一步。
            "completion_step": completion_step,
            "total_chunks": 0,
            "processed_chunks": 0,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "steps": [
                # 为步骤表中的每个 (key, label) 创建初始 pending 状态。
                {
                    "key": key,
                    "label": label,
                    "percent": 0,
                    "status": "pending",
                    "message": "",
                }
                for key, label in steps
            ],
        }
        with self._lock:
            # 新任务在锁内一次性发布，其他线程不会读到只填了一半的状态。
            self._jobs[job_id] = job
            return deepcopy(job)

    # 返回 deepcopy，调用方只能读取快照，不能绕过管理器修改内部任务。
    def get_job(self, job_id: str) -> dict | None:
        """根据任务 ID 获取当前状态快照。

        Args:
            job_id: create_job 返回的任务 ID。

        Returns:
            dict | None: 找到时返回深拷贝任务字典，不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def update_step(
        self,
        job_id: str,
        step_key: str,
        percent: int,
        status: StepStatus = "running",
        message: str = "",
        *,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
    ) -> dict | None:
        """更新某个步骤的进度、状态、消息及可选块处理计数。

        Args:
            job_id: 要更新的任务 ID。
            step_key: 要更新步骤的内部 key，例如 ``"parse"``。
            percent: 当前步骤进度百分比，会被限制到 0 至 100。
            status: 新步骤状态，默认 ``"running"``。
            message: 面向前端展示的当前步骤消息。
            total_chunks: 可选的总分块数量。
            processed_chunks: 可选的已处理分块数量。

        Returns:
            dict | None: 更新后的深拷贝快照；任务或步骤不存在时返回 None。
        """
        # 先把进度限制在 0–100，避免异常回调破坏前端进度条。
        percent = max(0, min(100, int(percent)))
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                # 任务 ID 不存在，调用方可将 None 解释为任务已失效或参数错误。
                return None

            step = self._find_step(job, step_key)
            if not step:
                # 找不到指定步骤时不修改整个任务。
                return None

            step["percent"] = percent
            step["status"] = status
            step["message"] = message
            # 步骤失败会立即提升为任务失败，普通更新则保持 running。
            job["status"] = "failed" if status == "failed" else "running"
            job["current_step"] = step_key
            job["message"] = message
            job["updated_at"] = _now_iso()

            if total_chunks is not None:
                # 显式传入 0 也应更新，因此使用 is not None 而非 if total_chunks。
                job["total_chunks"] = int(total_chunks)
            if processed_chunks is not None:
                job["processed_chunks"] = int(processed_chunks)

            return deepcopy(job)

    def complete_step(self, job_id: str, step_key: str, message: str = "") -> dict | None:
        """将一个步骤标记为 100% 完成。

        Args:
            job_id: 要更新的任务 ID。
            step_key: 已完成步骤的内部 key。
            message: 可选完成消息。

        Returns:
            dict | None: 更新后的任务快照；任务或步骤不存在时返回 None。
        """
        # 复用 update_step，避免完成状态的字段更新逻辑重复。
        return self.update_step(job_id, step_key, 100, "completed", message)

    # 完成任务时只补齐未失败步骤，不能把已经失败的步骤改写成成功。
    def complete_job(self, job_id: str, message: str = "文档入库完成") -> dict | None:
        """将整个任务标记为成功，并补齐尚未失败步骤的完成状态。

        Args:
            job_id: 要完成的任务 ID。
            message: 最终展示消息；上传和删除任务可传入不同文本。

        Returns:
            dict | None: 完成后的任务快照；任务不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for step in job["steps"]:
                if step["status"] != "failed":
                    # 未失败步骤统一显示为已完成，避免进度条停在中间。
                    step["percent"] = 100
                    step["status"] = "completed"
            job["status"] = "completed"
            job["current_step"] = job.get("completion_step") or job["current_step"]
            job["message"] = message
            job["error"] = None
            job["updated_at"] = _now_iso()
            return deepcopy(job)

    # 失败同时记录 step、message 和 error，轮询接口才能指出具体故障位置。
    def fail_job(self, job_id: str, step_key: str, error: str) -> dict | None:
        """标记整个任务失败，并记录失败步骤和错误消息。

        Args:
            job_id: 失败任务的 ID。
            step_key: 发生失败的步骤 key。
            error: 供前端和日志展示的错误描述。

        Returns:
            dict | None: 失败后的任务快照；任务不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            step = self._find_step(job, step_key)
            if step:
                # 只有步骤 key 合法时才更新步骤详情；任务本身仍会标记失败。
                step["status"] = "failed"
                step["message"] = error
            job["status"] = "failed"
            job["current_step"] = step_key
            job["message"] = error
            job["error"] = error
            job["updated_at"] = _now_iso()
            return deepcopy(job)

    def list_jobs(self) -> list[dict]:
        """返回当前进程中全部任务的深拷贝快照列表。"""
        with self._lock:
            return [deepcopy(job) for job in self._jobs.values()]

    @staticmethod
    def _find_step(job: dict, step_key: str) -> dict | None:
        """在一个任务的 steps 列表中按内部 key 查找步骤。

        Args:
            job: 内部任务字典。
            step_key: 要查找的步骤内部 key。

        Returns:
            dict | None: 对应步骤字典；不存在时返回 None。
        """
        for step in job["steps"]:
            if step["key"] == step_key:
                return step
        return None


# 上传路由与后台上传线程共用的全局任务管理器。
upload_job_manager = UploadJobManager()
# 删除路由与后台删除线程使用独立管理器，避免两类任务在同一个字典中混杂。
delete_job_manager = UploadJobManager()
