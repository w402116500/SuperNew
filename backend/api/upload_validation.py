"""上传文档的文件名校验工具。

这里只验证客户端提交的“文件名”是否安全且受支持；不接受路径，也不负责读取文件内容。
"""

# 元组保存允许上传的扩展名。元组不可修改，适合作为固定白名单常量。
# 该白名单应与 DocumentLoader 实际支持的 PDF、Word、Excel、HTML、Markdown、TXT 格式保持一致。
SUPPORTED_DOCUMENT_SUFFIXES = (
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".md",
    ".txt",
)


def is_supported_document(filename: str) -> bool:
    """判断一个客户端文件名是否安全，且扩展名属于支持的文档类型。

    Args:
        filename: 客户端上传时提供的原始文件名；允许传入空字符串或 None 等假值。

    Returns:
        bool: 文件名是安全的单个名称且后缀在白名单中时返回 True，否则返回 False。
    """
    # filename 为 None 等假值时先使用空字符串；strip() 去除用户输入的首尾空白。
    candidate = (filename or "").strip()
    # 文件名必须是单个名称，不能携带目录、绝对路径或过滤表达式分隔符。
    if not candidate or "/" in candidate or "\\" in candidate or '"' in candidate:
        # 空名称、Unix 路径分隔符、Windows 路径分隔符或双引号都视为非法。
        return False
    # "."、".." 具有目录语义；ASCII 码小于 32 的字符是不可见控制字符。
    if candidate in {".", ".."} or any(ord(char) < 32 for char in candidate):
        return False
    # lower() 让 .PDF 与 .pdf 都能通过；endswith(...) 检查是否以白名单中的任一后缀结束。
    return candidate.lower().endswith(SUPPORTED_DOCUMENT_SUFFIXES)


def normalize_upload_filename(filename: str) -> str:
    """验证并规范化上传文件名，失败时抛出适合 API 层处理的异常。

    Args:
        filename: 客户端提供的原始文件名。

    Returns:
        str: 已去除首尾空白、可安全用于临时文件的支持文件名。

    Raises:
        ValueError: 名称为空、包含路径/危险字符、包含控制字符或后缀不受支持时抛出。
    """
    # 与 is_supported_document 相同，先处理 None 并去除首尾空白。
    candidate = (filename or "").strip()
    if not candidate:
        # 空名称无法安全创建临时文件，也无法选择正确的解析器。
        raise ValueError("文件名不能为空")
    if "/" in candidate or "\\" in candidate or '"' in candidate or candidate in {".", ".."}:
        # 拒绝 ../secret.pdf、C:\\... 等路径穿越或非单文件名输入。
        raise ValueError("文件名不能包含路径或危险字符")
    # 控制字符在日志和文件系统中不可见，必须在写临时文件前拒绝。
    if any(ord(char) < 32 for char in candidate):
        raise ValueError("文件名包含非法控制字符")
    # 扩展名白名单与实际 loader 能力保持一致，避免上传后才发现无法解析。
    if not is_supported_document(candidate):
        raise ValueError("仅支持 PDF、Word、Excel、HTML、Markdown 和 TXT 文档")
    # 所有规则通过后，返回去除首尾空白后的安全文件名。
    return candidate
