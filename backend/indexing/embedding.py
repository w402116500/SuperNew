# 本模块将文本转换为浮点数向量，供 Milvus 进行语义相似度检索。
"""文本向量化服务：使用本地 Hugging Face 模型生成密集向量。

本服务只处理密集向量（dense vector）。Milvus 2.5+ 可另行提供中文分词和 BM25
全文检索能力，因此不在此处生成稀疏向量。
"""

# os 用于读取环境变量中的模型名称和运行设备配置。
import os
# Lock 防止并发请求在首次使用时重复加载数 GB 的模型权重。
from threading import Lock

# HuggingFaceEmbeddings 是 LangChain 对 SentenceTransformer/Hugging Face 嵌入模型的封装。
from langchain_huggingface import HuggingFaceEmbeddings


def _create_dense_embedder() -> HuggingFaceEmbeddings:
    """根据环境变量创建并配置一个 Hugging Face 密集向量模型。

    环境变量：
        EMBEDDING_MODEL: 要加载的模型名称；未设置时使用 ``BAAI/bge-m3``。
        EMBEDDING_DEVICE: 模型运行设备，例如 ``"cpu"``、``"cuda"``；未设置时使用 CPU。
        EMBEDDING_LOCAL_FILES_ONLY: 是否只从本地 Hugging Face 缓存加载模型；默认 ``true``。

    Returns:
        HuggingFaceEmbeddings: 已配置归一化选项的 LangChain 嵌入模型对象。
    """
    # getenv 的第二个参数是默认模型名；本地或环境变量未配置时会加载 BAAI/bge-m3。
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
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

    def _get_embedder(self) -> HuggingFaceEmbeddings:
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
            # embed_documents 接收文本列表，返回与 texts 等长的向量列表。
            # 例如 texts 有 2 项时，结果形式为 [[...第 1 个向量...], [...第 2 个向量...]]。
            return self._get_embedder().embed_documents(texts)
        except Exception as e:
            # 使用 from e 保留底层异常链，方便排查模型下载、设备或输入数据问题。
            raise Exception(f"本地密集嵌入模型调用失败: {str(e)}") from e


# 全进程唯一实例
# 模型初始化成本较高，因此全进程复用一个 EmbeddingService 实例。
# 其他模块通常使用：from backend.indexing.embedding import embedding_service。
embedding_service = EmbeddingService()
