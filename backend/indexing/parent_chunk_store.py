# 父块存储会将 L1/L2/L3 分块信息写入 PostgreSQL，并把热点数据缓存在 Redis。
"""父级分块文档存储（用于 Auto-merging Retriever）。

PostgreSQL 是可持久化的事实来源；Redis 是可丢失、可由数据库重新构建的读取缓存。
父块的 ``parent_chunk_id`` 与 ``root_chunk_id`` 用于从细粒度检索结果向上恢复完整上下文。
"""

# UTC 和 datetime 用于记录父块最后写入或更新的时间。
from datetime import UTC, datetime
# List 用于标注多个块 ID 或多个块字典组成的列表。
from typing import List

# cache 是项目封装的 Redis JSON 缓存对象。
from backend.infra.cache import cache
# SessionLocal 是创建 SQLAlchemy 数据库会话的工厂。
from backend.infra.database import SessionLocal
# ParentChunk 是 PostgreSQL 中 parent_chunks 表对应的 ORM 模型。
from backend.db.models import ParentChunk


class ParentChunkStore:
    """基于 PostgreSQL 和 Redis 的父级分块读写服务。

    PostgreSQL 保存块的长期数据；Redis 使用 ``parent_chunk:<chunk_id>`` 键缓存单个块，
    加快 Auto-merging 检索器根据块 ID 恢复父级文本的速度。
    """

    @staticmethod
    def _to_dict(item: ParentChunk) -> dict:
        """将 ParentChunk ORM 对象转换为可 JSON 序列化的普通字典。

        Args:
            item: 从 PostgreSQL 查询得到的一条 ``ParentChunk`` 记录。

        Returns:
            dict: 包含块文本、来源信息、层级和父子关系字段的字典。
        """
        return {
            # 当前 L1、L2 或 L3 块的实际正文。
            "text": item.text,
            # 引用展示时使用的原始文件名。
            "filename": item.filename,
            # 文件类别，例如 PDF、Word、Excel、HTML。
            "file_type": item.file_type,
            # 原始文件在磁盘或存储系统中的路径。
            "file_path": item.file_path,
            # PDF 页码或 HTML 章节序号。
            "page_number": item.page_number,
            # 当前块的稳定唯一 ID。
            "chunk_id": item.chunk_id,
            # 当前块的直接父块 ID；L1 根块通常为空字符串。
            "parent_chunk_id": item.parent_chunk_id,
            # 当前块所属最顶层 L1 根块 ID。
            "root_chunk_id": item.root_chunk_id,
            # 层级数字：1、2、3 分别代表 L1、L2、L3。
            "chunk_level": item.chunk_level,
            # 文件中的总体顺序编号。
            "chunk_idx": item.chunk_idx,
        }

    @staticmethod
    def _cache_key(chunk_id: str) -> str:
        """生成单个父块在 Redis 中使用的缓存键。

        Args:
            chunk_id: 当前块的稳定唯一 ID。

        Returns:
            str: 形如 ``parent_chunk:guide.pdf::p1::l2::0`` 的缓存键。
        """
        # 添加固定前缀，避免与会话缓存或其他业务缓存键发生冲突。
        return f"parent_chunk:{chunk_id}"

    def upsert_documents(self, docs: List[dict]) -> int:
        """批量插入或更新父级分块，并在成功提交后同步 Redis 缓存。

        ``upsert`` 意为“存在则更新，不存在则插入”。以稳定的 ``chunk_id`` 为判断依据，
        因此同一文件重复导入时不会无限创建重复父块。

        Args:
            docs: 由 DocumentLoader 生成的块字典列表；每项应包含 ``chunk_id``、``text``、
                文件元数据、层级和父子关系字段。

        Returns:
            int: 本次成功参与插入或更新的块数量；空列表时返回 0。

        Raises:
            Exception: 数据库写入或提交失败时回滚本批事务后继续抛出原异常。
        """
        if not docs:
            # 没有输入数据时无需打开数据库连接。
            return 0

        # 创建一次数据库会话，用一个事务处理当前批次的全部块。
        db = SessionLocal()
        # 记录本批有效块的数量，最终作为方法返回值。
        upserted = 0
        # 循环里只收集待发布的缓存副本，事务提交前不能让 Redis 看见新数据。
        cache_updates = []
        try:
            # 逐个处理调用方传入的分块字典。
            for doc in docs:
                # get(... ) 读取 chunk_id；or "" 处理 None；strip() 去除意外空白。
                chunk_id = (doc.get("chunk_id") or "").strip()
                # 没有稳定 chunk_id 的块无法更新或关联，直接跳过而不是生成随机键。
                if not chunk_id:
                    continue

                # 按主键查询是否已经保存过这个块。
                record = db.query(ParentChunk).filter(ParentChunk.chunk_id == chunk_id).first()
                # payload 只包含数据库模型中允许写入或更新的字段。
                payload = {
                    # 文本正文，缺失时使用空字符串。
                    "text": doc.get("text", ""),
                    # 来源文件名。
                    "filename": doc.get("filename", ""),
                    # 文件类型。
                    "file_type": doc.get("file_type", ""),
                    # 来源文件路径。
                    "file_path": doc.get("file_path", ""),
                    # or 0 处理 None 或空值，int(...) 将页码统一为整数。
                    "page_number": int(doc.get("page_number", 0) or 0),
                    # 直接父块 ID。
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    # 最顶层 L1 根块 ID。
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    # 当前块层级，统一保存为整数。
                    "chunk_level": int(doc.get("chunk_level", 0) or 0),
                    # 文件内总体顺序，统一保存为整数。
                    "chunk_idx": int(doc.get("chunk_idx", 0) or 0),
                    # 使用不带时区的 UTC 时间，匹配当前数据库 DateTime 字段定义。
                    "updated_at": datetime.now(UTC).replace(tzinfo=None),
                }
                # 缓存不需要 updated_at，但要包含后续读取父块所需的全部业务字段。
                cache_payload = {
                    # chunk_id 不在 payload 内，因此单独加入缓存字典。
                    "chunk_id": chunk_id,
                    "text": payload["text"],
                    "filename": payload["filename"],
                    "file_type": payload["file_type"],
                    "file_path": payload["file_path"],
                    "page_number": payload["page_number"],
                    "parent_chunk_id": payload["parent_chunk_id"],
                    "root_chunk_id": payload["root_chunk_id"],
                    "chunk_level": payload["chunk_level"],
                    "chunk_idx": payload["chunk_idx"],
                }
                # 已存在记录就更新字段，否则插入新行，从而支持同一文档的幂等重建。
                if record:
                    # setattr(record, key, value) 等价于逐个执行 record.text = ... 等赋值。
                    for key, value in payload.items():
                        setattr(record, key, value)
                else:
                    # **payload 将字典展开为 ParentChunk 构造函数的命名参数。
                    db.add(ParentChunk(chunk_id=chunk_id, **payload))

                # 暂存缓存键和值；必须等数据库 commit 成功后才能真正写入 Redis。
                cache_updates.append((self._cache_key(chunk_id), cache_payload))
                # 当前块已参与本批写入，计数加一。
                upserted += 1

            # 一次提交覆盖本批所有父块，任何一条失败都会进入 rollback。
            db.commit()
        # 回滚后继续抛出原异常，让上层任务把真正失败步骤记录下来。
        except Exception:
            # 撤销本事务中的新增、更新等未提交修改。
            db.rollback()
            # 不吞掉错误，交给调用方记录日志或告知任务失败。
            raise
        finally:
            # 无论提交成功还是失败，都关闭会话并归还连接。
            db.close()

        # 只有 commit 成功离开 try 后，才逐条发布 Redis 缓存。
        for cache_key, cache_payload in cache_updates:
            # set_json 会将字典序列化为 JSON 后写入 Redis。
            cache.set_json(cache_key, cache_payload)

        # 返回真正写入或更新过的有效块数量。
        return upserted

    def get_documents_by_ids(self, chunk_ids: List[str]) -> List[dict]:
        """按指定 ID 批量读取父块，优先从 Redis 缓存读取，不足部分再查询 PostgreSQL。

        Args:
            chunk_ids: 检索器给出的块 ID 列表。返回结果会尽量保持该列表的顺序。

        Returns:
            List[dict]: 找到的父块字典列表；空输入、空 ID 或不存在的 ID 不会出现在结果中。
        """
        if not chunk_ids:
            # 空 ID 列表无需访问 Redis 或数据库。
            return []

        # 字典先按 chunk_id 汇总缓存和数据库结果，最后再按输入列表恢复顺序。
        # 键为块 ID，值为对应块字典。
        ordered_results = {}
        # 收集 Redis 未命中的 ID，稍后一次性查询数据库，避免 N 次 SQL 查询。
        missing_ids = []
        # 保持遍历顺序，以便最后按调用方输入顺序返回。
        for chunk_id in chunk_ids:
            # 清理 None 或带空白的 ID。
            key = (chunk_id or "").strip()
            if not key:
                # 空 ID 没有查询意义，跳过。
                continue
            # 先尝试读取 Redis 中的单块缓存。
            cached = cache.get_json(self._cache_key(key))
            if cached:
                # 缓存命中，暂存结果，不需要查询数据库。
                ordered_results[key] = cached
            else:
                # 缓存未命中，记录下来以便后续批量查 PostgreSQL。
                missing_ids.append(key)

        # 只批量查询缓存未命中的 ID，已经命中的块不会重复访问 PostgreSQL。
        if missing_ids:
            # 只在确实有未命中项时创建数据库会话。
            db = SessionLocal()
            try:
                # in_(...) 对应 SQL 的 WHERE chunk_id IN (...)，一次取回所有缺失块。
                rows = db.query(ParentChunk).filter(ParentChunk.chunk_id.in_(missing_ids)).all()
                for row in rows:
                    # ORM 记录先转换为普通字典，便于返回和缓存。
                    payload = self._to_dict(row)
                    # 使用数据库中的稳定 chunk_id 作为汇总字典的键。
                    ordered_results[row.chunk_id] = payload
                    # 回填 Redis；同一块的下一次请求可直接命中缓存。
                    cache.set_json(self._cache_key(row.chunk_id), payload)
            finally:
                # 查询完成或发生异常时都关闭数据库会话。
                db.close()

        # 返回顺序必须与检索器给出的 ID 顺序一致，否则引用排名会被打乱。
        # 列表推导式会跳过未找到的 ID，并保留输入中的重复 ID（如果调用方传入重复项）。
        return [ordered_results[item] for item in chunk_ids if item in ordered_results]



    # 删除范围由服务端保存的 filename 决定，并先收集受影响的缓存键。
    def delete_by_filename(self, filename: str) -> int:
        """按文件名删除父级分块，返回删除条数。"""
        if not filename:
            return 0

        db = SessionLocal()
        try:
            rows = db.query(ParentChunk).filter(ParentChunk.filename == filename).all()
            chunk_ids = [row.chunk_id for row in rows]
            deleted = len(chunk_ids)
            if deleted > 0:
                db.query(ParentChunk).filter(ParentChunk.filename == filename).delete(synchronize_session=False)
                # 数据库删除提交后才逐个失效缓存，避免缓存先消失而事实仍在。
                db.commit()
                for chunk_id in chunk_ids:
                    cache.delete(self._cache_key(chunk_id))
            return deleted
        finally:
            db.close()
