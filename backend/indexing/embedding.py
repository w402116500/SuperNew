# 本模块将文本转换为浮点数向量，供 Milvus 进行语义相似度检索。
"""文本向量化服务：使用本地 Hugging Face 模型生成密集向量。

本服务只处理密集向量（dense vector）。Milvus 2.5+ 可另行提供中文分词和 BM25
全文检索能力，因此不在此处生成稀疏向量。
"""

# os 用于读取环境变量中的模型名称、provider 和 API 配置。
import os
import time
from collections import deque
# Lock 防止并发请求在首次使用时重复加载数 GB 的模型权重。
from threading import Lock

# HuggingFaceEmbeddings 是 LangChain 对 SentenceTransformer/Hugging Face 嵌入模型的封装。
from langchain_huggingface import HuggingFaceEmbeddings
# OpenAIEmbeddings 兼容 SiliconFlow 的 OpenAI 风格 Embeddings API。
from langchain_openai import OpenAIEmbeddings


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"


def _embedding_provider() -> str:
    """读取并规范化密集向量 provider，默认保持本地模型行为。"""
    provider = os.getenv("EMBEDDING_PROVIDER", "huggingface").strip().lower()
    if provider in {"huggingface", "hf", "local"}:
        return "huggingface"
    if provider in {"siliconflow", "silicon-flow", "sf"}:
        return "siliconflow"
    raise ValueError(
        "不支持的 EMBEDDING_PROVIDER："
        f"{provider!r}；可选值为 huggingface 或 siliconflow"
    )


def _embedding_batch_size() -> int:
    raw_value = os.getenv("EMBEDDING_BATCH_SIZE", "50").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_BATCH_SIZE 必须是正整数") from exc
    if value <= 0:
        raise ValueError("EMBEDDING_BATCH_SIZE 必须是正整数")
    return value


def embedding_public_config() -> dict[str, str]:
    """返回可写入评测快照的 Embedding 配置，不包含任何密钥。"""
    provider = _embedding_provider()
    return {
        "embedding_provider": provider,
        "embedding_model": os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "embedding_base_url": (
            os.getenv("EMBEDDING_BASE_URL", DEFAULT_SILICONFLOW_BASE_URL)
            if provider == "siliconflow"
            else ""
        ),
        "embedding_batch_size": str(_embedding_batch_size()),
        "embedding_max_rpm": os.getenv("EMBEDDING_MAX_RPM", "2000"),
        "embedding_max_tpm": os.getenv("EMBEDDING_MAX_TPM", "500000"),
    }


def _read_embedding_timeout() -> float:
    raw_value = os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60").strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_TIMEOUT_SECONDS 必须是正数") from exc
    if value <= 0:
        raise ValueError("EMBEDDING_TIMEOUT_SECONDS 必须是正数")
    return value


def _create_dense_embedder() -> HuggingFaceEmbeddings | OpenAIEmbeddings:
    """根据环境变量创建本地或 SiliconFlow 密集向量模型。

    环境变量：
        EMBEDDING_PROVIDER: ``huggingface``（默认）或 ``siliconflow``。
        EMBEDDING_MODEL: 要加载的模型名称；未设置时使用 ``BAAI/bge-m3``。
        EMBEDDING_API_KEY: SiliconFlow API 密钥；也兼容 ``SILICONFLOW_API_KEY``。
        EMBEDDING_BASE_URL: SiliconFlow OpenAI 兼容地址；默认 ``https://api.siliconflow.cn/v1``。
        EMBEDDING_BATCH_SIZE: 远程 API 单批文本数；默认 50。
        EMBEDDING_TIMEOUT_SECONDS: 远程 API 请求超时；默认 60 秒。
        EMBEDDING_DEVICE: 模型运行设备，例如 ``"cpu"``、``"cuda"``；未设置时使用 CPU。
        EMBEDDING_LOCAL_FILES_ONLY: 是否只从本地 Hugging Face 缓存加载模型；默认 ``true``。

    Returns:
        LangChain 嵌入模型对象。远程 provider 使用 OpenAI 兼容客户端。
    """
    config = embedding_public_config()
    if config["embedding_provider"] == "siliconflow":
        # 不把 key 写入配置快照；支持通用名称和更直观的 SiliconFlow 专用名称。
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("SILICONFLOW_API_KEY")
        if not api_key:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=siliconflow 时必须设置 EMBEDDING_API_KEY "
                "或 SILICONFLOW_API_KEY"
            )
        return OpenAIEmbeddings(
            model=config["embedding_model"],
            api_key=api_key,
            base_url=config["embedding_base_url"],
            chunk_size=int(config["embedding_batch_size"]),
            timeout=_read_embedding_timeout(),
            # 上游三级分块已控制文本长度；关闭 LangChain 的 tiktoken 分词，
            # 让 SiliconFlow 收到原始文本，而不是 OpenAI 专用 token ID 列表。
            check_embedding_ctx_length=False,
        )

    # 本地或环境变量未配置时会加载 BAAI/bge-m3。
    model_name = config["embedding_model"]
    # device 控制模型在哪种硬件上运行；cuda 可使用 NVIDIA GPU，cpu 最通用但通常较慢。
    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    # BGE-M3 没有 processor_config.json。新版 Transformers 在允许联网时会先探测该可选
    # 文件，网络不可用时会导致加载失败；本项目默认使用已下载的本地模型缓存。
    local_files_only = os.getenv("EMBEDDING_LOCAL_FILES_ONLY", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }

    # 创建 LangChain 的模型封装。首次调用或首次实例化时可能下载/加载模型权重。
    return HuggingFaceEmbeddings(
        # Hugging Face Hub 上的模型名称或本地模型目录。
        model_name=model_name,
        # model_kwargs 会传给底层模型加载器；默认不进行联网探测。
        model_kwargs={"device": device, "local_files_only": local_files_only},
        # 向量归一化后才能稳定使用内积比较相似度；索引和查询必须采用同一设置。
        # normalize_embeddings=True 会让每个向量长度约为 1。
        encode_kwargs={"normalize_embeddings": True},
    )


class EmbeddingService:
    """对外提供批量文本向量化功能的服务类。

    调用示例：
        ``vectors = EmbeddingService().get_embeddings(["第一段文本", "第二段文本"])``

    返回结果是二维列表：外层列表对应输入文本顺序，内层列表是每段文本的浮点数向量。
    """

    def __init__(self, state_path=None):
        """初始化服务并加载底层密集向量模型。

        Args:
            state_path: 预留的兼容参数，当前实现未使用；模型配置来自环境变量。
        """
        # state_path 当前不参与逻辑，保留该参数可避免旧调用代码因参数不匹配而失效。
        # 延后加载：应用可以在模型缓存暂不可用时仍提供登录、文档管理等接口；首次需要
        # 向量化时才加载模型。
        self._embedder = None
        self._embedder_lock = Lock()
        self._rate_limit_lock = Lock()
        self._request_times: deque[float] = deque()
        self._token_events: deque[tuple[float, int]] = deque()

    @staticmethod
    def _limit(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(value, 0)

    def _wait_for_rate_limit(self, texts: list[str]) -> None:
        """Apply process-local RPM/TPM limits before a provider request."""
        max_rpm = self._limit("EMBEDDING_MAX_RPM", 2000)
        max_tpm = self._limit("EMBEDDING_MAX_TPM", 500000)
        if max_rpm == 0 and max_tpm == 0:
            return
        estimated_tokens = sum(len(text) for text in texts)
        while True:
            now = time.monotonic()
            with self._rate_limit_lock:
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()
                while self._token_events and now - self._token_events[0][0] >= 60:
                    self._token_events.popleft()
                request_blocked = max_rpm > 0 and len(self._request_times) >= max_rpm
                token_total = sum(value for _, value in self._token_events)
                token_blocked = (
                    max_tpm > 0
                    and token_total + estimated_tokens > max_tpm
                    and bool(self._token_events)
                )
                if not request_blocked and not token_blocked:
                    self._request_times.append(now)
                    self._token_events.append((now, estimated_tokens))
                    return
                waits = []
                if request_blocked:
                    waits.append(60 - (now - self._request_times[0]))
                if token_blocked:
                    waits.append(60 - (now - self._token_events[0][0]))
                delay = max(0.01, min(waits))
            time.sleep(delay)

    def _get_embedder(self) -> HuggingFaceEmbeddings | OpenAIEmbeddings:
        """按需加载并缓存底层模型，保证并发首次调用只初始化一次。"""
        if self._embedder is not None:
            return self._embedder

        with self._embedder_lock:
            if self._embedder is None:
                self._embedder = _create_dense_embedder()
        return self._embedder

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """批量将文本转换为归一化的密集向量。

        Args:
            texts: 待向量化的文本列表。每个字符串会按原顺序生成一个向量。

        Returns:
            list[list[float]]: 二维浮点数列表。例如输入两段文本，会返回两个向量。

        Raises:
            Exception: 底层模型加载、推理或编码失败时，抛出带业务语境的异常。
        """
        # 空批次直接返回，避免为没有内容的任务加载或调用模型。
        if not texts:
            return []
        try:
            self._wait_for_rate_limit(texts)
            # embed_documents 接收文本列表，返回与 texts 等长的向量列表。
            # 例如 texts 有 2 项时，结果形式为 [[...第 1 个向量...], [...第 2 个向量...]]。
            return self._get_embedder().embed_documents(texts)
        except Exception as e:
            # 使用 from e 保留底层异常链，方便排查本地模型或远程 API 问题。
            raise Exception(f"密集嵌入调用失败: {str(e)}") from e


# 全进程唯一实例
# 模型初始化成本较高，因此全进程复用一个 EmbeddingService 实例。
# 其他模块通常使用：from backend.indexing.embedding import embedding_service。
embedding_service = EmbeddingService()
