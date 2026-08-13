"""Single source of truth for supported knowledge-base document formats."""

RICH_DOCUMENT_SUFFIXES = (
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
)

NATIVE_TEXT_SUFFIXES = (
    ".md",
    ".txt",
    ".html",
    ".htm",
)

SUPPORTED_DOCUMENT_SUFFIXES = RICH_DOCUMENT_SUFFIXES + NATIVE_TEXT_SUFFIXES


def is_rich_document(filename: str) -> bool:
    return (filename or "").lower().endswith(RICH_DOCUMENT_SUFFIXES)


def document_type_for_filename(filename: str) -> str:
    file_lower = (filename or "").lower()
    if file_lower.endswith(".pdf"):
        return "PDF"
    if file_lower.endswith(".docx"):
        return "Word"
    if file_lower.endswith(".pptx"):
        return "PowerPoint"
    if file_lower.endswith(".xlsx"):
        return "Excel"
    if file_lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")):
        return "Image"
    if file_lower.endswith((".html", ".htm")):
        return "HTML"
    if file_lower.endswith(".md"):
        return "Markdown"
    if file_lower.endswith(".txt"):
        return "Text"
    raise ValueError(f"Unsupported document type: {filename}")
