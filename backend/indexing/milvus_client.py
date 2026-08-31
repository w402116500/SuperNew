# 本模块集中封装 Milvus 的建集合、写入、查询、检索和删除操作。
"""Milvus 访问层：无状态 Store 与短生命周期连接。

MilvusStore 本身不长期保存网络连接；每次 IO 创建连接、执行操作、随后关闭，避免数据库
重启或网络变化后复用已经失效的 gRPC channel。
"""

# annotations 让类型标注中的 MilvusSettings | None 等写法可以延迟解析。
from __future__ import annotations

# os 用于读取 Milvus 主机、端口、集合名、超时等环境变量。
import os
import json
# contextmanager 用于把“创建连接 → 使用 → 关闭连接”写成 with 语句。
from contextlib import contextmanager
# dataclass 用于简洁定义 MilvusSettings 配置数据类。
from dataclasses import dataclass
# Callable、Iterator、TypeVar 用于为回调和上下文管理器添加泛型类型标注。
from typing import Callable, Iterator, TypeVar

# AnnSearchRequest 表示一次向量搜索请求；DataType 定义字段类型；
# MilvusClient 是客户端；RRFRanker 合并混合检索结果；Function/FunctionType 配置 BM25。
from pymilvus import AnnSearchRequest, DataType, MilvusClient, RRFRanker, Function, FunctionType

from backend.indexing.chunk_metadata import STRUCTURED_CHUNK_METADATA_FIELDS

# 单次 Milvus query 的最大返回数量，分页查询会以此值分批拉取。
QUERY_MAX_LIMIT = 16384
# 泛型 T 表示 _run 传入的操作返回什么类型，_run 就返回同样类型。
T = TypeVar("T")
_DOCUMENT_OUTPUT_FIELDS = [
    "text",
    "filename",
    "file_type",
    "page_number",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
    "chunk_idx",
    *STRUCTURED_CHUNK_METADATA_FIELDS,
]


@dataclass(frozen=True)
class MilvusSettings:
    """Milvus 连接与集合配置。

    frozen=True 表示实例创建后字段不可再修改，避免运行过程中意外改变连接地址。

    Attributes:
        host: Milvus 服务主机名或 IP。
        port: Milvus 服务端口。
        collection_name: 默认操作的集合名称。
        uri: 拼接好的客户端连接地址。
        timeout: 单次客户端操作的超时时间，单位秒。
    """
    # 服务地址主机部分，例如 localhost 或 Docker 服务名 standalone。
    host: str
    # 服务端口，使用字符串以便直接参与 URI 拼接。
    port: str
    # 保存向量和元数据的 Milvus collection 名称。
    collection_name: str
    # 形如 http://localhost:19530 的完整连接地址。
    uri: str
    # 网络请求超时秒数。
    timeout: float
    # 云端或开启鉴权的 Milvus 访问令牌；本地未鉴权实例为空。
    token: str = ""

    @classmethod
    def from_env(cls) -> MilvusSettings:
        """从环境变量读取配置，并在缺失时使用本地开发默认值。

        Returns:
            MilvusSettings: 包含主机、端口、集合名、URI 和超时配置的新实例。
        """
        # 第二个参数是默认值，未配置环境变量时连接本机 Docker 暴露的 Milvus。
        host = os.getenv("MILVUS_HOST", "localhost")
        port = os.getenv("MILVUS_PORT", "19530")
        collection = os.getenv("MILVUS_COLLECTION", "embeddings_collection")
        # 环境变量读取到的是字符串，float(...) 转换为超时秒数。
        timeout = float(os.getenv("MILVUS_TIMEOUT", "30"))
        uri = os.getenv("MILVUS_URI", "").strip() or f"http://{host}:{port}"
        token = os.getenv("MILVUS_TOKEN", "").strip()
        # cls(...) 在类方法中表示当前类，方便未来子类继承此构造逻辑。
        return cls(
            host=host,
            port=port,
            collection_name=collection,
            uri=uri,
            timeout=timeout,
            token=token,
        )


@contextmanager
def milvus_client_session(settings: MilvusSettings | None = None) -> Iterator[MilvusClient]:
    """创建一次 Milvus 客户端会话，并在使用完成后始终关闭连接。

    Args:
        settings: 可选的连接配置；不传时从环境变量创建默认配置。

    Yields:
        MilvusClient: 可在 ``with`` 代码块中执行 Milvus 操作的客户端。
    """
    # 调用方提供配置时优先使用，否则读取当前环境变量。
    cfg = settings or MilvusSettings.from_env()
    # 使用 URI 和超时创建客户端；此对象内部会管理到 Milvus 的网络连接。
    client_kwargs = {"uri": cfg.uri, "timeout": cfg.timeout}
    if cfg.token:
        client_kwargs["token"] = cfg.token
    client = MilvusClient(**client_kwargs)
    try:
        # 将客户端交给 with 代码块使用，并在此处暂停函数。
        yield client
    finally:
        # 无论查询成功还是抛出异常，都关闭连接，避免长期持有失效 channel。
        client.close()


def _normalize_filter(filter_expr: str) -> str:
    """规范化 Milvus 标量过滤表达式，保证 query 总能收到有效过滤条件。

    Args:
        filter_expr: 调用方传入的 Milvus 过滤表达式，可为空字符串。

    Returns:
        str: 去除首尾空白的原表达式；空表达式时返回匹配所有正常 ID 的 ``"id >= 0"``。
    """
    # strip() 去掉首尾空白；空条件使用恒真的 id >= 0 代替。
    return filter_expr.strip() if filter_expr.strip() else "id >= 0"


class MilvusStore:
    """Milvus 集合的读写服务。

    服务对象只保存 ``MilvusSettings``，不保存 ``MilvusClient``。每个操作通过 ``_run``
    使用短生命周期连接；批量业务流程可使用 ``session()`` 显式复用一条连接。
    """

    def __init__(self, settings: MilvusSettings | None = None):
        """初始化 Store 并固定本实例使用的 Milvus 配置。

        Args:
            settings: 可选配置对象，适合测试或连接不同实例；不传时从环境变量读取。
        """
        # 将配置保存在实例中，后续方法统一使用同一集合与连接地址。
        self._settings = settings or MilvusSettings.from_env()

    @property
    def collection_name(self) -> str:
        """返回当前 Store 默认操作的 Milvus 集合名称。"""
        # 对外只读暴露配置中的 collection_name。
        return self._settings.collection_name

    def _run(self, operation: Callable[[MilvusClient], T]) -> T:
        """在短生命周期 Milvus 连接中执行一个操作回调。

        Args:
            operation: 接收 ``MilvusClient`` 并返回任意类型结果的函数。

        Returns:
            T: operation 的原始返回值。
        """
        # 进入上下文时创建客户端，离开时自动关闭客户端。
        with milvus_client_session(self._settings) as client:
            # 把刚创建的 client 传给具体查询、插入或删除逻辑。
            return operation(client)

    @contextmanager
    def session(self) -> Iterator[MilvusClient]:
        """在同一个业务流程内复用一条 Milvus 连接，用毕即关闭。

        Yields:
            MilvusClient: 调用方可在 ``with store.session() as client`` 中复用的客户端。
        """
        # 将底层会话继续包装为 Store 的公共上下文管理器。
        with milvus_client_session(self._settings) as client:
            yield client

    @staticmethod
    def ensure_collection(client: MilvusClient, collection_name: str, dense_dim: int) -> None:
        """若集合不存在，则创建 schema、BM25 函数以及向量索引。

        Args:
            client: 已连接的 Milvus 客户端。
            collection_name: 要确保存在的集合名称。
            dense_dim: 密集向量的维度，必须与嵌入模型输出维度一致。

        Returns:
            None: 集合已存在时直接返回；不存在时创建完成后返回。
        """
        # has_collection 为 True 说明 schema 和索引已经存在，无需重复创建。
        if client.has_collection(collection_name):
            return

        # auto_id=True 让 Milvus 自动生成主键；enable_dynamic_field=True 允许写入额外动态字段。
        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        # id 是 Milvus 自动生成的 INT64 主键。
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        # dense_embedding 保存嵌入模型生成的固定维度浮点向量。
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        # sparse_embedding 保存 BM25 函数从 text 自动生成的稀疏向量。
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        # text 是正文；开启中文 analyzer 和 match，供 BM25 全文检索使用。
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
            enable_match=True,
        )
        # 以下字段保存文件来源、页码和 DocumentLoader 生成的三层块关系。
        schema.add_field("filename", DataType.VARCHAR, max_length=255)
        schema.add_field("file_type", DataType.VARCHAR, max_length=50)
        schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("page_number", DataType.INT64)
        schema.add_field("chunk_idx", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_level", DataType.INT64)

        # 定义 Milvus 内置 BM25 函数：输入 text，输出到 sparse_embedding。
        bm25_function = Function(
            name="text_bm25_emb",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse_embedding"],
        )
        # 将 BM25 函数注册到 schema，插入 text 时 Milvus 会生成对应稀疏向量。
        schema.add_function(bm25_function)

        # prepare_index_params 创建索引配置收集器。
        index_params = client.prepare_index_params()
        # 为密集向量建立 HNSW 近似最近邻索引；IP（内积）适合已归一化的向量。
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 256},
        )
        # 为 BM25 稀疏向量建立倒排索引；drop_ratio_build 用于丢弃极低权重项以节省资源。
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"drop_ratio_build": 0.2},
        )
        # 一次性按 schema 和两种索引创建集合。
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )

    def init_collection(self, dense_dim: int | None = None) -> None:
        """初始化默认集合；未传维度时从环境变量读取。

        Args:
            dense_dim: 嵌入向量维度。None 时读取 ``DENSE_EMBEDDING_DIM``，默认 1024。
        """
        if dense_dim is None:
            # 环境变量为字符串，需要转换为整数。
            dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        # 内部函数接收 _run 提供的短生命周期 client。
        def _init(client: MilvusClient) -> None:
            self.ensure_collection(client, self.collection_name, dense_dim)

        # 执行初始化，并在结束后自动关闭连接。
        self._run(_init)

    def insert(self, data: list[dict]):
        """向默认集合插入一批向量及其元数据。

        Args:
            data: Milvus 行字典列表；通常每项包含 dense_embedding、text 与块元数据。

        Returns:
            Any: pymilvus ``insert`` 的原始结果。
        """
        # 使用 lambda 把具体插入操作交给 _run 管理连接生命周期。
        return self._run(lambda client: client.insert(self.collection_name, data))

    def query(
        self,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
        limit: int = 10000,
        offset: int = 0,
    ):
        """执行一次 Milvus 标量查询。

        Args:
            filter_expr: Milvus 过滤表达式，例如 ``file_type == "PDF"``。
            output_fields: 需要返回的字段列表；不传时只返回 filename、file_type。
            limit: 最多返回多少行，会被限制在 QUERY_MAX_LIMIT 内。
            offset: 跳过前多少行，用于分页。

        Returns:
            Any: pymilvus ``query`` 返回的记录列表。
        """
        # 确保空过滤条件也会变成有效表达式。
        expr = _normalize_filter(filter_expr)
        # 调用方未指定字段时，只读取两个轻量来源字段。
        fields = output_fields or ["filename", "file_type"]

        def _query(client: MilvusClient):
            # min 防止调用方请求超过 Milvus 单次查询的最大限制。
            return client.query(
                collection_name=self.collection_name,
                filter=expr,
                output_fields=fields,
                limit=min(limit, QUERY_MAX_LIMIT),
                offset=offset,
            )

        return self._run(_query)

    def query_all(self, filter_expr: str = "", output_fields: list[str] | None = None) -> list:
        """分页读取符合过滤条件的所有记录，整个分页过程只使用一条连接。

        Args:
            filter_expr: Milvus 标量过滤表达式；空字符串表示查询全部。
            output_fields: 需要返回的字段；不传时返回 filename、file_type。

        Returns:
            list: 所有分页结果拼接后的列表。
        """
        fields = output_fields or ["filename", "file_type"]
        expr = _normalize_filter(filter_expr)

        def _query_all(client: MilvusClient) -> list:
            # out 累积所有页面的查询结果。
            out: list = []
            # offset 从第 0 行开始，后续每次增加本批返回数量。
            offset = 0
            while True:
                batch = client.query(
                    collection_name=self.collection_name,
                    filter=expr,
                    output_fields=fields,
                    limit=QUERY_MAX_LIMIT,
                    offset=offset,
                )
                if not batch:
                    # 没有数据表示已读完。
                    break
                # 将本页记录逐项加入总结果。
                out.extend(batch)
                if len(batch) < QUERY_MAX_LIMIT:
                    # 最后一页不足最大数量，无需继续请求下一页。
                    break
                # 下一页从当前已返回记录之后开始。
                offset += len(batch)
            return out

        return self._run(_query_all)

    def query_iterator(
        self,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
        batch_size: int = 1000,
        limit: int = -1,
    ):
        """Stream scalar-query batches without offset pagination.

        Milvus limits ``offset + limit`` to 16,384 for ``query``.  The native
        iterator uses a server-side cursor, so large collections can be read
        past that window without losing or repeating rows.
        """
        if batch_size <= 0:
            raise ValueError("Milvus query_iterator batch_size 必须是正整数")
        fields = output_fields or ["filename", "file_type"]
        expr = _normalize_filter(filter_expr)

        with milvus_client_session(self._settings) as client:
            iterator = client.query_iterator(
                collection_name=self.collection_name,
                filter=expr,
                output_fields=fields,
                batch_size=min(batch_size, QUERY_MAX_LIMIT),
                limit=limit,
            )
            try:
                while True:
                    batch = iterator.next()
                    if not batch:
                        break
                    yield batch
            finally:
                iterator.close()

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """根据块 ID 列表从 Milvus 读取对应块及其父子关系元数据。

        Args:
            chunk_ids: 要查询的 chunk_id 列表。

        Returns:
            list[dict]: 找到的块记录；空输入时返回空列表。
        """
        # 过滤 None、空字符串等无效 ID。
        ids = [item for item in chunk_ids if item]
        if not ids:
            return []
        # 用 JSON 字符串字面量转义 ID，避免文件名中的引号破坏 Milvus IN 表达式。
        quoted_ids = ", ".join(json.dumps(item, ensure_ascii=False) for item in ids)
        # 复用通用 query()，只取恢复上下文所需字段。
        return self.query(
            filter_expr=f"chunk_id in [{quoted_ids}]",
            output_fields=_DOCUMENT_OUTPUT_FIELDS,
            limit=len(ids),
        )

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        query: str,
        top_k: int = 5,
        rrf_k: int = 60,
        filter_expr: str = "",
    ) -> list[dict]:
        """同时执行密集语义检索与 BM25 关键词检索，并使用 RRF 合并排名。

        Args:
            dense_embedding: 查询文本通过嵌入模型生成的密集向量。
            query: 原始查询字符串，供 BM25 稀疏检索使用。
            top_k: 最终最多返回的结果数量。
            rrf_k: Reciprocal Rank Fusion 的平滑常数，越大越平滑。
            filter_expr: 可选 Milvus 标量过滤表达式。

        Returns:
            list[dict]: 统一格式的检索结果，包含块元数据和融合后的 score。
        """
        # output_fields 指定最终命中结果中需要带回的业务字段。
        output_fields = _DOCUMENT_OUTPUT_FIELDS
        # 第一条搜索请求：对 dense_embedding 字段执行内积语义相似度检索。
        dense_search = AnnSearchRequest(
            # Milvus 批量接口要求外层列表，即使当前只有一个查询向量。
            data=[dense_embedding],
            anns_field="dense_embedding",
            param={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        # 第二条搜索请求：把原始文本传给 BM25 稀疏向量字段进行关键词检索。
        sparse_search = AnnSearchRequest(
            data=[query],
            anns_field="sparse_embedding",
            param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        # RRF 根据两种检索各自的排名合并结果，而不是直接比较两种不相同的分数尺度。
        reranker = RRFRanker(k=rrf_k)

        def _search(client: MilvusClient):
            # hybrid_search 同时执行两条请求，再通过 ranker 取最终 top_k。
            return client.hybrid_search(
                collection_name=self.collection_name,
                reqs=[dense_search, sparse_search],
                ranker=reranker,
                limit=top_k,
                output_fields=output_fields,
            )

        results = self._run(_search)
        # 将 pymilvus 返回的 Hit 对象转换为项目统一的普通字典。
        formatted_results = []
        # results 外层通常对应查询批次；这里只有一个查询，但仍按通用结构遍历。
        for hits in results:
            for hit in hits:
                formatted_results.append({
                    # Milvus 自动生成的主键。
                    "id": hit.get("id"),
                    # 以下字段来自 output_fields；get 的默认值避免字段缺失时报错。
                    "text": hit.get("text", ""),
                    "filename": hit.get("filename", ""),
                    "file_type": hit.get("file_type", ""),
                    "page_number": hit.get("page_number", 0),
                    "chunk_id": hit.get("chunk_id", ""),
                    "parent_chunk_id": hit.get("parent_chunk_id", ""),
                    "root_chunk_id": hit.get("root_chunk_id", ""),
                    "chunk_level": hit.get("chunk_level", 0),
                    "chunk_idx": hit.get("chunk_idx", 0),
                    "score": hit.get("distance", 0.0),
                    **{
                        field: hit[field]
                        for field in STRUCTURED_CHUNK_METADATA_FIELDS
                        if field in hit
                    },
                })
        return formatted_results

    def dense_retrieve(
        self,
        dense_embedding: list[float],
        top_k: int = 5,
        filter_expr: str = "",
    ) -> list[dict]:
        """只使用密集向量执行语义相似度检索。

        Args:
            dense_embedding: 查询文本的密集向量。
            top_k: 最多返回的结果数量。
            filter_expr: 可选 Milvus 标量过滤表达式。

        Returns:
            list[dict]: 统一格式的纯密集向量检索结果。
        """
        def _search(client: MilvusClient):
            # search 是单路向量检索；data 外层列表表示一个查询向量的批次。
            return client.search(
                collection_name=self.collection_name,
                data=[dense_embedding],
                anns_field="dense_embedding",
                search_params={"metric_type": "IP", "params": {"ef": 64}},
                limit=top_k,
                output_fields=_DOCUMENT_OUTPUT_FIELDS,
                filter=filter_expr,
            )

        results = self._run(_search)
        # 与 hybrid_retrieve 一样，把结果转换为普通字典。
        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append({
                    # dense search 的业务字段位于 hit["entity"] 中，因此先取空字典兜底。
                    "id": hit.get("id"),
                    "text": hit.get("entity", {}).get("text", ""),
                    "filename": hit.get("entity", {}).get("filename", ""),
                    "file_type": hit.get("entity", {}).get("file_type", ""),
                    "page_number": hit.get("entity", {}).get("page_number", 0),
                    "chunk_id": hit.get("entity", {}).get("chunk_id", ""),
                    "parent_chunk_id": hit.get("entity", {}).get("parent_chunk_id", ""),
                    "root_chunk_id": hit.get("entity", {}).get("root_chunk_id", ""),
                    "chunk_level": hit.get("entity", {}).get("chunk_level", 0),
                    "chunk_idx": hit.get("entity", {}).get("chunk_idx", 0),
                    "score": hit.get("distance", 0.0),
                    **{
                        field: hit.get("entity", {})[field]
                        for field in STRUCTURED_CHUNK_METADATA_FIELDS
                        if field in hit.get("entity", {})
                    },
                })
        return formatted_results

    def bm25_retrieve(
        self,
        query: str,
        top_k: int = 5,
        filter_expr: str = "",
    ) -> list[dict]:
        """只使用 Milvus 原生 BM25 稀疏向量执行关键词检索。

        ``sparse_embedding`` 由集合 schema 中的 BM25 Function 从 ``text`` 自动生成，
        查询时直接传入原始文本即可，不在客户端重复维护分词或稀疏向量统计。
        """
        output_fields = _DOCUMENT_OUTPUT_FIELDS

        def _search(client: MilvusClient):
            return client.search(
                collection_name=self.collection_name,
                data=[query],
                anns_field="sparse_embedding",
                search_params={"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
                limit=top_k,
                output_fields=output_fields,
                filter=filter_expr,
            )

        results = self._run(_search)
        formatted_results = []
        for hits in results:
            for hit in hits:
                entity = hit.get("entity", {}) or {}
                formatted_results.append({
                    "id": hit.get("id"),
                    "text": hit.get("text", entity.get("text", "")),
                    "filename": hit.get("filename", entity.get("filename", "")),
                    "file_type": hit.get("file_type", entity.get("file_type", "")),
                    "page_number": hit.get("page_number", entity.get("page_number", 0)),
                    "chunk_id": hit.get("chunk_id", entity.get("chunk_id", "")),
                    "parent_chunk_id": hit.get("parent_chunk_id", entity.get("parent_chunk_id", "")),
                    "root_chunk_id": hit.get("root_chunk_id", entity.get("root_chunk_id", "")),
                    "chunk_level": hit.get("chunk_level", entity.get("chunk_level", 0)),
                    "chunk_idx": hit.get("chunk_idx", entity.get("chunk_idx", 0)),
                    "score": hit.get("distance", 0.0),
                    **{
                        field: hit.get(field, entity.get(field))
                        for field in STRUCTURED_CHUNK_METADATA_FIELDS
                        if field in hit or field in entity
                    },
                })
        return formatted_results

    def delete(self, filter_expr: str):
        """删除默认集合中匹配过滤条件的记录。

        Args:
            filter_expr: 必填 Milvus 过滤表达式，例如 ``filename == "old.pdf"``。

        Returns:
            Any: pymilvus ``delete`` 的原始执行结果。
        """
        # 将删除调用交给 _run，以确保连接被正确关闭。
        return self._run(
            lambda client: client.delete(collection_name=self.collection_name, filter=filter_expr)
        )

    def has_collection(self) -> bool:
        """检查当前默认集合是否已经存在。

        Returns:
            bool: 集合存在返回 True，否则返回 False。
        """
        # 使用短连接执行存在性检查。
        return self._run(lambda client: client.has_collection(self.collection_name))

    def drop_collection(self) -> None:
        """删除当前默认集合；集合不存在时不执行任何操作。

        Returns:
            None: 删除或跳过后返回。
        """
        def _drop(client: MilvusClient) -> None:
            # 先检查存在性，避免对不存在集合调用删除。
            if client.has_collection(self.collection_name):
                # 删除集合会删除其中的全部向量和索引。
                client.drop_collection(self.collection_name)

        # 执行删除，并在结束后自动关闭连接。
        self._run(_drop)


# 模块级缓存：延迟创建并复用 MilvusStore 配置对象（但不复用网络连接）。
_store: MilvusStore | None = None


def get_milvus_store() -> MilvusStore:
    """获取全进程共享的 MilvusStore 实例。

    Returns:
        MilvusStore: 首次调用时创建、后续调用时复用的 Store 对象。
    """
    # global 表示下面赋值的是模块变量 _store，而不是函数局部变量。
    global _store
    if _store is None:
        # 延迟实例化，直到真正需要使用 Milvus 时才读取配置并创建 Store。
        _store = MilvusStore()
    # 返回同一个 Store；每个实际 IO 仍会由 _run 创建新的客户端连接。
    return _store
