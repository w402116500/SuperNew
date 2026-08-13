"""LangChain Agent 可调用的工具（@tool 装饰的函数）。"""

# 包出口只导出工具工厂和天气工具，导入时不会执行检索或网络请求。
from backend.tools.knowledge import make_search_knowledge_base
from backend.tools.weather import get_current_weather_tool as get_current_weather

__all__ = [
    "get_current_weather",
    "make_search_knowledge_base",
]
