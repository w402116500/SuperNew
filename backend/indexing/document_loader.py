"""将 PDF、Word、Excel、HTML、Markdown 等文件统一转换为三层知识库文本块。

加载后的每个文本块都是字典，包含来源文件、页码、文本、块 ID，以及 L1/L2/L3
父子关系信息，供后续写入 PostgreSQL、Milvus 和 Auto-merging 检索流程使用。
"""

# os 用于遍历文件夹、组合文件路径。
import os
# re 用于匹配和替换控制字符、私有区字符。
import re
# unicodedata 用于将语义相同但编码形式不同的字符统一为 NFC。
import unicodedata
# SimpleNamespace 用于把 Markdown/TXT 这类纯文本包装成类似 LangChain Document 的对象。
from types import SimpleNamespace
# Dict、List 用于标注返回的文档块字典和列表类型。
from typing import Dict, List

# 这些 LangChain Loader 分别负责读取 Word、PDF、Excel 文件。
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, UnstructuredExcelLoader
# RecursiveCharacterTextSplitter 会尽量按自然分隔符切分长文本。
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 编译需要移除的 C0 控制字符与 DEL（保留常规排版字：\t、\n、\r）。
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 编译零宽字符和不可见格式化控制字符（零宽空白、BOM 标记、左右强排标志等）
_INVISIBLE_CHAR_RE = re.compile(r"[\u200b-\u200d\ufeff\u200f\u202a-\u202e]")


# 所有格式先经过同一文本清洗入口，避免非法字符直到写数据库时才报错。
def sanitize_text(text: str) -> str:
    """清洗文本中的不可见、非法或可能导致存储失败的 Unicode 字符。

    Args:
        text: 原始文本，例如文件正文、文件名或文件路径。

    Returns:
        str: 经 NFC 规范化、移除控制字符和无效代理项后的 UTF-8 可编码文本；空输入返回空字符串。

    处理步骤：
        1. 统一为 NFC 编码形式。
        2. 删除零宽字符、BOM 和双向文字控制符。
        3. 删除不可打印控制字符及 Unicode 私有使用区字符。
        4. 丢弃无法安全编码为 UTF-8 的孤立代理项。
    """
    if not text:
        # 空字符串不需要继续清洗，直接返回。
        return ""

    # 1. 规范化为 Unicode NFC 格式
    # NFC 会把视觉上相同、底层编码不同的字符统一成一种表示，利于搜索和去重。
    text = unicodedata.normalize("NFC", text)

    # 2. 清除不可见零宽字符、BOM 及格式控制符
    text = _INVISIBLE_CHAR_RE.sub("", text)

    # 3. 清洗非打印控制符及 PUA（Private Use Area，私有使用区）字符
    # sub("", text) 用空字符串替换所有匹配到的控制字符。
    text = _CONTROL_CHAR_RE.sub("", text)
    # U+E000 至 U+F8FF 是私有使用区，常见于来源不明的文档字体或乱码。
    text = re.sub(r"[\ue000-\uf8ff]", "", text)

    # 4. 通过 UTF-8 编码/解码丢弃孤立代理项，保证结果可写入 PostgreSQL UTF-8 文本列。
    try:
        # encode 先把 str 转为 UTF-8 字节；ignore 会跳过无法编码的字符。
        cleaned = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    except Exception:
        # 正常情况下上面的 ignore 不会失败；此分支作为兜底，逐字符跳过代理项范围。
        chars = []
        # 逐个检查每个字符，作为极端异常情况下的手动清洗方案。
        for char in text:
            # ord(char) 返回字符的 Unicode 码点；D800-DFFF 是 UTF-16 代理项区间。
            if 0xD800 <= ord(char) <= 0xDFFF:
                # 孤立代理项不是有效文本，跳过它。
                continue
            # 正常字符加入新列表。
            chars.append(char)
        # join 将字符列表重新合并成字符串。
        cleaned = "".join(chars)

    # 返回最终可安全保存和检索的文本。
    return cleaned


class DocumentLoader:
    """加载多种文件格式，并构建保留父子关系的 L1/L2/L3 文本块。

    Args:
        chunk_size: L3 叶子块的建议最大字符数。L1、L2 会按此值放大，并设有最低值。
        chunk_overlap: 相邻块之间保留的重叠字符数，避免句子在边界被截断后丢失上下文。

    Attributes:
        _splitter_level_1: 最大的根块切分器，保留较长上下文。
        _splitter_level_2: 中等大小的子块切分器。
        _splitter_level_3: 最小的叶子块切分器，通常用于向量检索。
    """

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100):
        """使用指定的基础大小和重叠大小初始化三个文本切分器。

        Args:
            chunk_size: L3 的基础块大小，默认 800 个字符左右。
            chunk_overlap: L3 相邻块的基础重叠大小，默认 100 个字符左右。
        """
        # 这里的“大小”和“重叠”默认按 Python len(text) 计算，也就是字符数，
        # 不是大模型的 Token 数。中文汉字、英文字符、标点和空格都会占用长度。
        # L1 最大、L2 居中、L3 最小，三个层级不是把同一组块简单复制三次。
        # 后续会在 L1 内切 L2、在 L2 内切 L3，形成真正的父子关系。
        # max 保证即使调用方传入很小的 chunk_size，L1 仍至少保留 2000 字符上下文。
        level_1_size = max(2000, chunk_size * 3)
        # L1 相邻大块至少共享 400 个字符，减少大上下文在边界断裂的概率。
        # 例如前一块在某个条款中间结束时，下一块会保留这段结尾作为开头。
        level_1_overlap = max(400, chunk_overlap * 3)
        # L2 的目标大小为 L3 的两倍，且不小于 1000。
        level_2_size = max(1000, chunk_size * 2)
        # L2 的重叠大小至少为 200。
        level_2_overlap = max(200, chunk_overlap * 2)
        # L3 使用调用方的基础大小，但最低保持 600。
        # 800、1600、2400 是每块的目标上限，不保证每块恰好这么长：
        # 切分器会优先在段落或句号处切开，因此实际块通常可能更短。
        level_3_size = max(600, chunk_size)
        # L3 使用调用方的基础重叠大小，但最低保持 100。
        level_3_overlap = max(100, chunk_overlap)

        # 每层 splitter 都保留 start_index，并使用相同的中文优先分隔顺序。
        # add_start_index=True 会在 LangChain 文档 metadata 中记录块在原文中的起始位置。
        # separators 从段落、句号到字符逐步尝试，尽量避免在中文句子中间切断。
        # 第一个切分器专门生成 L1 根块。
        self._splitter_level_1 = RecursiveCharacterTextSplitter(
            # L1 单块的目标最大字符数；本次默认值为 2400。
            chunk_size=level_1_size,
            # 相邻 L1 块之间重叠的字符数；本次默认值为 400。
            chunk_overlap=level_1_overlap,
            # 保存本块在原文本中的起始下标。
            add_start_index=True,
            # 按优先级依次尝试的中文自然分隔符。
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )
        # 第二个切分器只在每个 L1 块内部生成 L2 子块。
        self._splitter_level_2 = RecursiveCharacterTextSplitter(
            # L2 单块的目标最大字符数；本次默认值为 1600。
            chunk_size=level_2_size,
            # 相邻 L2 块之间重叠的字符数；本次默认值为 200。
            chunk_overlap=level_2_overlap,
            # 保存 L2 块的起始下标。
            add_start_index=True,
            # 使用与其他层级一致的分隔符优先级。
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )
        # 第三个切分器只在每个 L2 块内部生成最小的 L3 叶子块。
        self._splitter_level_3 = RecursiveCharacterTextSplitter(
            # L3 单块的目标最大字符数；本次默认值为 800。
            chunk_size=level_3_size,
            # 相邻 L3 块之间重叠的字符数；本次默认值为 100。
            chunk_overlap=level_3_overlap,
            # 保存 L3 块的起始下标。
            add_start_index=True,
            # 使用与 L1、L2 相同的分隔策略。
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )

    # 文件名、页码、层级和序号共同组成稳定 ID，后续才能准确恢复父子关系。
    @staticmethod
    def _build_chunk_id(filename: str, page_number: int, level: int, index: int) -> str:
        """根据文件位置和层级构造稳定且可读的块 ID。

        Args:
            filename: 来源文件名。
            page_number: 来源页码或 HTML 章节序号。
            level: 当前块层级，使用 1、2、3 分别表示 L1、L2、L3。
            index: 当前页和当前层级中的顺序编号。

        Returns:
            str: 类似 ``guide.pdf::p2::l3::5`` 的块 ID。
        """
        # f-string 会把四个参数插入固定格式，形成可复现的文本 ID。
        return f"{filename}::p{page_number}::l{level}::{index}"

    def _split_page_to_three_levels(
        self,
        text: str,
        base_doc: Dict,
        page_global_chunk_idx: int,
    ) -> List[Dict]:
        """将一个页面/章节文本递归切分为具有父子关系的 L1、L2、L3 块。

        关系结构为：``L1 根块 → 多个 L2 子块 → 每个 L2 下多个 L3 叶子块``。

        Args:
            text: 当前页面或章节的已清洗正文文本。
            base_doc: 每个块都要继承的基础元数据，至少含 ``filename``、``file_path``、
                ``file_type`` 和 ``page_number``。
            page_global_chunk_idx: 当前文件中已生成块的数量，用于为本页块分配连续序号。

        Returns:
            List[Dict]: 三层块字典列表。每项含 ``text``、``chunk_id``、
            ``parent_chunk_id``、``root_chunk_id``、``chunk_level`` 和 ``chunk_idx``。
        """
        # 空页面不产生任何层级块，避免写入无法检索的空记录。
        if not text:
            return []

        # 尽管变量名为 root_chunks，列表实际会收集 L1、L2、L3 三个层级的全部块。
        root_chunks: List[Dict] = []
        # 从基础元数据读取页码；缺失时使用 0。
        page_number = int(base_doc.get("page_number", 0))
        # 文件名用于构造每个块的稳定 ID。
        filename = base_doc["filename"]

        # create_documents 接收文本列表和对应 metadata 列表，返回 LangChain Document 列表。
        level_1_docs = self._splitter_level_1.create_documents([text], [base_doc])
        # 每个层级单独计数，确保同一文件、页码和层级下的 chunk_id 不重复。
        level_1_counter = 0
        # L2 计数器独立于 L1，生成的 L2 ID 不会重复。
        level_2_counter = 0
        # L3 计数器独立于 L1、L2。
        level_3_counter = 0

        # 先创建 L1 根块；同一根块下的所有后代共享 root_chunk_id。
        for level_1_doc in level_1_docs:
            # page_content 是 LangChain Document 的正文；or "" 处理空值；strip 去首尾空白。
            level_1_text = (level_1_doc.page_content or "").strip()
            if not level_1_text:
                # 空的 L1 块无需保存，也不继续向下切分。
                continue
            # 用当前 L1 计数器构造根块 ID。
            level_1_id = self._build_chunk_id(filename, page_number, 1, level_1_counter)
            # ID 已被使用，递增计数器供下一个 L1 块使用。
            level_1_counter += 1

            level_1_chunk = {
                # **base_doc 把文件名、路径、类型、页码复制到当前块。
                **base_doc,
                # 当前 L1 的实际文本。
                "text": level_1_text,
                # 当前块的唯一稳定 ID。
                "chunk_id": level_1_id,
                # L1 是根块，没有直接父块，因此使用空字符串。
                "parent_chunk_id": "",
                # 根块的 root ID 就是自身 ID。
                "root_chunk_id": level_1_id,
                # 数字 1 表示 L1 层级。
                "chunk_level": 1,
                # 文件内的全局顺序编号。
                "chunk_idx": page_global_chunk_idx,
            }
            # 下一个生成的块使用下一个全局序号。
            page_global_chunk_idx += 1
            # 将完整 L1 块加入最终结果列表。
            root_chunks.append(level_1_chunk)

            # 只在当前 L1 的文本范围内切 L2，保证父子块内容真正嵌套。
            level_2_docs = self._splitter_level_2.create_documents([level_1_text], [base_doc])
            # L2 必须在当前 L1 文本内部继续切分，因此它的 parent_chunk_id 就是当前 level_1_id。
            for level_2_doc in level_2_docs:
                # 读取并清理当前 L2 文本。
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    # 空 L2 不产生数据，也不继续生成 L3。
                    continue
                # 用当前 L2 计数器构造其唯一 ID。
                level_2_id = self._build_chunk_id(filename, page_number, 2, level_2_counter)
                # 递增 L2 计数器。
                level_2_counter += 1

                level_2_chunk = {
                    **base_doc,
                    # 当前 L2 的正文文本。
                    "text": level_2_text,
                    # 当前 L2 的唯一 ID。
                    "chunk_id": level_2_id,
                    # L2 的直接父块是包裹它的当前 L1。
                    "parent_chunk_id": level_1_id,
                    # L2 仍属于同一个 L1 根块。
                    "root_chunk_id": level_1_id,
                    # 数字 2 表示 L2 层级。
                    "chunk_level": 2,
                    # 文件内的全局顺序编号。
                    "chunk_idx": page_global_chunk_idx,
                }
                # 为下一个块准备新的全局序号。
                page_global_chunk_idx += 1
                # 将 L2 块加入最终结果列表。
                root_chunks.append(level_2_chunk)

                # 同理，只在当前 L2 文本中切 L3，L3 是后续最细粒度的检索单位。
                level_3_docs = self._splitter_level_3.create_documents([level_2_text], [base_doc])
                # L3 在当前 L2 内切分，直接父块指向 L2，同时保留 L1 根 ID 供多级上卷。
                for level_3_doc in level_3_docs:
                    # 读取并清理当前 L3 文本。
                    level_3_text = (level_3_doc.page_content or "").strip()
                    if not level_3_text:
                        # 空 L3 不保存。
                        continue
                    # 用当前 L3 计数器构造叶子块 ID。
                    level_3_id = self._build_chunk_id(filename, page_number, 3, level_3_counter)
                    # 递增 L3 计数器。
                    level_3_counter += 1
                    # L3 没有子块，但仍记录直接父块 L2 和最顶层根块 L1。
                    root_chunks.append({
                        **base_doc,
                        # 当前 L3 的实际文本，通常用于细粒度向量检索。
                        "text": level_3_text,
                        # 当前 L3 的唯一 ID。
                        "chunk_id": level_3_id,
                        # L3 的直接父块是刚创建的 L2。
                        "parent_chunk_id": level_2_id,
                        # L3 向上追溯时仍可定位到最初的 L1 根块。
                        "root_chunk_id": level_1_id,
                        # 数字 3 表示 L3 层级。
                        "chunk_level": 3,
                        # 文件内的全局顺序编号。
                        "chunk_idx": page_global_chunk_idx,
                    })
                    # 为下一个块准备新的全局序号。
                    page_global_chunk_idx += 1

        # 返回该页面或章节的所有层级块。
        return root_chunks

    def _load_plain_text_document(self, file_path: str, filename: str, doc_type: str) -> list[dict]:
        """读取 Markdown/TXT 纯文本文件，并复用统一的三层分块流程。"""
        with open(file_path, "r", encoding="utf-8-sig") as file:
            text = file.read()

        raw_docs = [SimpleNamespace(page_content=text, metadata={"page": 0})]
        return self._load_from_langchain_docs(raw_docs, file_path, filename, doc_type)

    # 不同 loader 的输出从这里进入同一套 metadata 和三级分块流程。
    def _load_from_langchain_docs(
        self,
        raw_docs: list,
        file_path: str,
        filename: str,
        doc_type: str,
    ) -> list[dict]:
        """将任意 LangChain Loader 的输出统一清洗、补充元数据并进行三层切分。

        Args:
            raw_docs: Loader 返回的 LangChain Document 列表。
            file_path: 原始文件在本地磁盘的路径。
            filename: 原始文件名，用于生成块 ID 和引用来源。
            doc_type: 文件类型标记，例如 ``"PDF"``、``"Word"``、``"Excel"``、``"HTML"``。

        Returns:
            list[dict]: 所有页面/章节生成的三层块字典列表。
        """
        # documents 会聚合一个文件中所有页或章节产生的全部块。
        documents: list[dict] = []
        # 在同一文件内连续编号，便于后续保持稳定的原始顺序。
        page_global_chunk_idx = 0
        for doc in raw_docs:
            # 不同 Loader 的 metadata 结构可能不同，因此使用 getattr 和空字典兜底。
            meta = getattr(doc, "metadata", None) or {}
            # 外部 loader 的页码可能缺失或类型异常，先规范化成整数再参与 chunk_id。
            page_num = meta.get("page", 0)
            if page_num is None:
                # 某些 Loader 会显式设置 page=None，此时统一按第 0 页处理。
                page_num = 0
            try:
                # 页码可能是字符串，例如 "2"，先转换为整数。
                page_num = int(page_num)
            except (TypeError, ValueError):
                # 转换失败（例如 "unknown"）时安全回退为 0。
                page_num = 0
            base_doc = {
                # 文件相关字段也经过 sanitize_text，避免文件名或路径的隐藏字符进入数据库。
                "filename": sanitize_text(filename),
                "file_path": sanitize_text(file_path),
                "file_type": sanitize_text(doc_type),
                "page_number": page_num,
            }
            # page_content 是当前页或章节正文；先处理 None、首尾空白，再执行文本清洗。
            page_chunks = self._split_page_to_three_levels(
                text=sanitize_text((doc.page_content or "").strip()),
                base_doc=base_doc,
                page_global_chunk_idx=page_global_chunk_idx,
            )
            # 该页产生多少块，就为下一页预留多少个全局编号。
            page_global_chunk_idx += len(page_chunks)
            # extend 将当前页面生成的多个块逐个加入总列表；append 则会嵌套成一个列表。
            documents.extend(page_chunks)
        return documents

    def load_document(self, file_path: str, filename: str) -> list[dict]:
        """根据文件扩展名选择解析器，加载一个文件并返回统一的三层块列表。

        Args:
            file_path: 待加载文件的实际本地路径。
            filename: 文件名，通常是 ``os.path.basename(file_path)``；用于选择格式和构造块 ID。

        Returns:
            list[dict]: 该文件产生的所有 L1/L2/L3 文本块。

        Raises:
            ValueError: 文件扩展名不属于当前支持的 PDF、Word、Excel、HTML、Markdown、TXT 类型。
            Exception: 底层 Loader 读取或解析文件失败。
        """
        # lower() 使扩展名判断不区分大小写，例如 .PDF 与 .pdf 都能识别。
        file_lower = filename.lower()

        # 扩展名只负责选择解析器，解析完成后的数据结构保持一致。
        if file_lower.endswith(".pdf"):
            # PDF 文档使用 LangChain 的 PyPDFLoader。
            doc_type = "PDF"
            loader = PyPDFLoader(file_path)
        elif file_lower.endswith((".docx", ".doc")):
            # Word 文档使用基于 docx2txt 的加载器。
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
        elif file_lower.endswith((".xlsx", ".xls")):
            # Excel 工作簿使用 UnstructuredExcelLoader。
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path)
        # HTML 使用前面实现的语义清洗器，而不是通用文件 loader。
        elif file_lower.endswith((".html", ".htm")):
            doc_type = "HTML"
            # 延迟导入避免在非 HTML 场景加载 BeautifulSoup 相关模块，也避免潜在循环依赖。
            from backend.indexing.html_processor import load_html_for_document_loader

            # HTML 解析器已经返回 LangChain Document 列表，无需调用 loader.load()。
            raw_docs = load_html_for_document_loader(file_path, filename)
            return self._load_from_langchain_docs(raw_docs, file_path, filename, doc_type)
        elif file_lower.endswith(".md"):
            doc_type = "Markdown"
            return self._load_plain_text_document(file_path, filename, doc_type)
        elif file_lower.endswith(".txt"):
            doc_type = "Text"
            return self._load_plain_text_document(file_path, filename, doc_type)
        else:
            # 目前没有相应解析器的扩展名直接提示调用方。
            raise ValueError(f"不支持的文件类型: {filename}")

        try:
            # PDF、Word、Excel Loader 都通过 load() 返回 LangChain Document 列表。
            raw_docs = loader.load()
            return self._load_from_langchain_docs(raw_docs, file_path, filename, doc_type)
        # 保留原异常作为 cause，并补充文件处理语境，排错时能同时看到两层信息。
        except Exception as e:
            # 使用 from e 保留底层异常链，便于定位是文件损坏还是 Loader 配置问题。
            raise Exception(f"处理文档失败: {str(e)}") from e

    def load_documents_from_folder(self, folder_path: str) -> list[dict]:
        """遍历文件夹中支持的文件，逐个加载并合并全部文本块。

        Args:
            folder_path: 要扫描的文件夹路径。当前只扫描此目录，不递归进入子目录。

        Returns:
            list[dict]: 所有成功加载文件产生的三层块字典列表。

        Notes:
            不支持的文件和加载失败的文件会被跳过，以便批量导入可以继续处理剩余文件。
        """
        # all_documents 用于累积每个成功文件返回的块。
        all_documents = []

        # listdir() 返回文件夹第一层中的文件名和子文件夹名。
        for filename in os.listdir(folder_path):
            # 用小写副本判断扩展名，避免大小写差异导致文件漏处理。
            file_lower = filename.lower()
            if not (
                file_lower.endswith(".pdf")
                or file_lower.endswith((".docx", ".doc"))
                or file_lower.endswith((".xlsx", ".xls"))
                or file_lower.endswith((".html", ".htm"))
                or file_lower.endswith((".md", ".txt"))
            ):
                # 扩展名不受支持时跳过该条目。
                continue

            # join() 组合目录与文件名，避免手动拼接路径分隔符。
            file_path = os.path.join(folder_path, filename)
            try:
                # 复用单文件入口，保证批量与单文件使用同一套解析和分块规则。
                documents = self.load_document(file_path, filename)
                all_documents.extend(documents)
            except Exception:
                # 当前策略是忽略单个坏文件，继续处理文件夹中的其他文件。
                continue

        # 返回文件夹中所有成功文件生成的全部块。
        return all_documents
