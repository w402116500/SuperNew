"""
HTML 知识库处理：编码检测、去脚本/样式、语义线性化，并按章节拆成多「页」供三层分块复用。

设计要点：
- 优先解析 <main>/<article>，否则 <body>，避免全站导航噪声。
- 将 h1–h4 与段落、列表项按文档顺序线性化，标题转为 Markdown 风格前缀。
- 若存在多个二级标题区块，按区块拆成多个 page_number，改善 chunk_id 分散与可解释性。
"""

from __future__ import annotations

# re 用于使用正则表达式识别 HTML 中声明的字符编码。
import re
# Path 让文件路径操作在 Windows、macOS、Linux 上保持一致。
from pathlib import Path
# Any 用于标注 BeautifulSoup 标签等动态类型对象。
from typing import Any

# BeautifulSoup 负责解析 HTML；Comment 代表 HTML 注释节点。
from bs4 import BeautifulSoup
from bs4.element import Comment
# Document 是 LangChain 的标准文档对象，后续可交给分块、向量化等流程。
from langchain_core.documents import Document


# 先识别 BOM 和页面声明的 charset，最后才回退 GB18030，避免中文网页一开始就被错误解码。
def _read_html_text(path: Path) -> str:
    """读取 HTML 文件，并尽量使用正确的字符编码解码为 Python 字符串。

    解码优先级：UTF-8 BOM → HTML ``charset`` 声明 → UTF-8 → GB18030。

    Args:
        path: 要读取的本地 HTML 文件路径。

    Returns:
        str: 已解码的 HTML 文本。
    """
    # read_bytes() 读取原始二进制数据；此时还没有决定使用什么文本编码。
    raw = path.read_bytes()
    # UTF-8 BOM 是文件开头的三个特殊字节。发现后去掉 BOM 并按 UTF-8 解码。
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")

    # 只读取文件前 8192 字节来找 <meta charset="...">，无需先解码整个大文件。
    # errors="ignore" 表示暂时忽略无效 UTF-8 字节，因为这里只是为了搜索编码声明。
    head = raw[:8192].decode("utf-8", errors="ignore")
    # re.I 表示忽略大小写，可同时匹配 charset、CHARSET 等写法。
    m = re.search(r'charset\s*=\s*["\']?([^"\'>\s]+)', head, re.I)
    if m:
        # group(1) 是正则中第一个括号捕获到的编码名称，例如 "utf-8" 或 "gbk"。
        enc = m.group(1).strip().lower()
        # Python 使用 "utf-8" 这个标准名称，将网页常见的 "utf8" 统一为它。
        if enc == "utf8":
            enc = "utf-8"
        try:
            # 优先使用网页自身声明的编码解码完整文件。
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            # LookupError：Python 不认识该编码名；UnicodeDecodeError：编码声明与实际内容不符。
            # 此时不让解析失败，继续尝试后备编码。
            pass
    try:
        # 没有可用声明时，现代网页最常见的默认编码是 UTF-8。
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # GB18030 兼容常见的 GBK/GB2312 中文网页。
        # errors="replace" 会将无法解码的字节替换为特殊字符，而不是直接抛出异常。
        return raw.decode("gb18030", errors="replace")


# 脚本、样式和注释不属于知识正文，在分块前统一移除。
def _strip_noise(soup: BeautifulSoup) -> None:
    """就地删除 HTML 中不应参与知识库检索的噪声节点。

    Args:
        soup: 已解析的 BeautifulSoup HTML 文档对象。该对象会被直接修改。

    Returns:
        None: 函数通过修改 ``soup`` 生效，不返回新的对象。
    """
    # soup([...]) 查找多个标签名；decompose() 会从 DOM 树中彻底删除标签及其子节点。
    for tag in soup(["script", "style", "noscript", "template", "iframe", "svg"]):
        tag.decompose()
    # find_all 找到所有文本节点；lambda 只保留类型为 HTML 注释的节点。
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        # extract() 将当前注释节点从 DOM 树中移除。
        c.extract()


# 优先选择 main 或 article，减少导航栏、页脚等站点公共噪声。
def _pick_root(soup: BeautifulSoup) -> Any:
    """选择最可能包含文章正文的 HTML 根节点。

    Args:
        soup: 已去除噪声的 BeautifulSoup 文档对象。

    Returns:
        Any: 依次返回 ``<main>``、``<article>``、``<body>``；都不存在时返回整个文档。
    """
    # Python 的 or 会返回第一个“非空”结果，因此顺序即为正文优先级。
    return soup.find("main") or soup.find("article") or soup.body or soup


def _br_to_newlines(root: Any) -> None:
    """将正文中的 ``<br>`` 标签原地替换为换行符。

    Args:
        root: 需要处理的 BeautifulSoup 标签或文档节点。

    Returns:
        None: ``root`` 会被原地修改。
    """
    # 否则 get_text() 处理 <br> 时可能把两段文字连在一起。
    for br in root.find_all("br"):
        br.replace_with("\n")


# 按 DOM 顺序把标题和段落线性化，检索片段才能保留原文阅读顺序。
def _linearize_blocks(root: Any) -> str:
    """按原始 DOM 顺序提取常见正文块，并生成适合分块的线性文本。

    ``h1`` 至 ``h4`` 会转换为 Markdown 标题，其他块保留为普通文本；若提取结果
    明显过短，则回退为正文的完整纯文本，避免漏掉不规则网页中的主要内容。

    Args:
        root: 已选择的正文根节点，例如 ``<main>``、``<article>`` 或 ``<body>``。

    Returns:
        str: 带 Markdown 标题层级的正文文本，或必要时回退得到的纯文本。
    """
    # 先保留 HTML 中手动换行的语义。
    _br_to_newlines(root)

    # 只遍历这些常见的正文块标签，避免把所有嵌套 span、div 等都重复提取出来。
    block_tags = [
        "h1",
        "h2",
        "h3",
        "h4",
        "p",
        "li",
        "blockquote",
        "pre",
        "td",
        "th",
        "dd",
        "dt",
        "figcaption",
    ]
    # parts 会按文档顺序收集每个文本块，最后再合并为一个字符串。
    parts: list[str] = []
    # limit=8000 防止异常或超大网页生成过多节点，导致处理时间过长。
    for el in root.find_all(block_tags, limit=8000):
        # 双重保险：即使噪声标签未被提前删除，也不要提取其内部文字。
        if el.find_parent(["script", "style", "noscript"]):
            continue
        # separator=" " 防止标签之间的文字直接黏连；strip=True 去除首尾空白。
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        name = el.name
        # 将 HTML 标题转换为 Markdown 标题，保留文档的层级结构，便于后续检索和显示。
        if name == "h1":
            parts.append(f"\n# {text}\n")
        elif name == "h2":
            parts.append(f"\n## {text}\n")
        elif name == "h3":
            parts.append(f"\n### {text}\n")
        elif name == "h4":
            parts.append(f"\n#### {text}\n")
        else:
            parts.append(text)

    # 将提取到的块以换行合并；strip() 去掉首尾多余空白。
    structured = "\n".join(parts).strip()
    # plain 是不区分块类型的完整纯文本，作为结构化提取不完整时的后备结果。
    plain = root.get_text("\n", strip=True)

    # 两种提取方式之一为空时，直接使用另一种。
    if not plain:
        return structured
    if not structured:
        return plain

    # 结构化结果过短通常表示标签未覆盖正文，此时退回完整纯文本以免静默丢内容。
    if len(structured) < max(120, int(0.22 * len(plain))):
        return plain

    return structured


def _doc_title(soup: BeautifulSoup) -> str:
    """读取 HTML ``<title>`` 标签中的页面标题。

    Args:
        soup: 已解析的 BeautifulSoup HTML 文档对象。

    Returns:
        str: 去除首尾空白的标题；页面没有标题时返回空字符串。
    """
    # find("title") 返回第一个 <title> 标签；不存在时返回 None。
    t = soup.find("title")
    if t and t.string:
        return t.string.strip()
    return ""


# 二级标题被视为章节边界，每个章节生成独立 page_number。
def _split_into_sections(linear: str, page_title: str) -> list[dict[str, Any]]:
    """按 Markdown 二级标题将线性正文拆分为多个章节字典。

    Args:
        linear: ``_linearize_blocks`` 返回的线性正文。
        page_title: HTML 页面标题，用于单章节页面的标题和内容前缀。

    Returns:
        list[dict[str, Any]]: 每项包含 ``page``（章节序号）、``title``（章节标题）和
        ``text``（章节正文）。无正文时返回空列表。
    """
    # 去掉首尾空白，空页面无需产生章节。
    text = linear.strip()
    if not text:
        return []

    # (?m) 开启多行模式；(?=...) 是零宽前瞻：在每行 "## " 标题之前切分，但不丢掉标题。
    pieces = re.split(r"(?m)^(?=## .+$)", text)
    # 去除空块，例如正文开头或连续分隔符产生的空字符串。
    pieces = [p.strip() for p in pieces if p.strip()]
    if len(pieces) <= 1:
        # 没有多个二级标题时，整页作为第 1 个章节；可选地在内容前加入网页标题。
        head = f"[{page_title}]\n\n" if page_title else ""
        return [{"page": 1, "title": page_title or "", "text": head + text}]

    # 有多个二级标题时，每个块生成一个带独立 page 序号的章节。
    sections: list[dict[str, Any]] = []
    for i, block in enumerate(pieces, start=1):
        # split(..., 1) 最多切一次，得到当前章节的第一行，用于识别章节标题。
        first_line = block.split("\n", 1)[0].strip()
        sec_title = ""
        if first_line.startswith("## "):
            sec_title = first_line[3:].strip()
        elif i == 1 and first_line.startswith("# ") and not first_line.startswith("##"):
            # 第一个块可能从页面一级标题开始，也把它作为该块标题。
            sec_title = first_line[2:].strip()
        # 在正文前插入可读章节前缀，帮助后续检索结果显示上下文。
        prefix = f"[章节: {sec_title}]\n\n" if sec_title else ""
        sections.append({"page": i, "title": sec_title, "text": prefix + block})
    return sections


def parse_html_file_to_sections(file_path: str | Path) -> list[dict[str, Any]]:
    """解析一个 HTML 文件，清理噪声后按章节返回文本。

    Args:
        file_path: HTML 文件路径；可传入字符串或 ``pathlib.Path`` 对象。

    Returns:
        list[dict[str, Any]]: 章节字典列表，每项含 ``page``、``title`` 和 ``text``。
    """
    # Path(...) 将 str 或 Path 统一转换为 Path 对象。
    path = Path(file_path)
    # 依次执行：读取解码 → 解析 HTML → 删除噪声 → 找正文 → 线性化 → 按章节拆分。
    html = _read_html_text(path)
    soup = BeautifulSoup(html, "html.parser")
    _strip_noise(soup)
    root = _pick_root(soup)
    page_title = _doc_title(soup)
    linear = _linearize_blocks(root)
    # 正文没有标题时补入网页 title，让后续小块仍携带页面主题。
    if page_title and linear and not linear.lstrip().startswith("#"):
        linear = f"# {page_title}\n\n{linear}"
    return _split_into_sections(linear, page_title)


def load_html_for_document_loader(file_path: str, filename: str) -> list[Document]:
    """将 HTML 文件转换为可交给 LangChain 文档加载流程的 ``Document`` 列表。

    Args:
        file_path: 本地 HTML 文件的完整路径，用于读取实际内容。
        filename: 对外显示的原始文件名，写入每个 Document 的 ``metadata.source``。

    Returns:
        list[Document]: 每个章节对应一个 Document；其 ``page_content`` 是章节文本，
        ``metadata`` 中包含章节序号、来源文件名和章节标题。
    """
    # 先复用上面的解析流程，得到已清理且已拆分的章节字典。
    sections = parse_html_file_to_sections(file_path)
    # 用于收集最终的 LangChain Document 对象。
    docs: list[Document] = []
    for sec in sections:
        docs.append(
            # 最终统一成 LangChain Document，metadata.page 保存章节序号供引用展示。
            Document(
                page_content=sec["text"],
                metadata={
                    "page": sec["page"],
                    "source": filename,
                    "section_title": sec.get("title") or "",
                },
            )
        )
    return docs
