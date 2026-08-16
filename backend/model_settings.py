"""共享的模型调用运行时配置。"""

from __future__ import annotations

import os


def _positive_seconds(name: str, default: str) -> float:
    """读取正数秒数配置，并为无效值提供明确的配置错误。"""
    try:
        return max(float(os.getenv(name, default)), 1.0)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是大于 0 的秒数") from exc


def model_timeout_seconds() -> float:
    """读取单次模型调用的硬超时，避免兼容服务无限等待。"""
    return _positive_seconds("MODEL_TIMEOUT_SECONDS", "90")


def evaluation_case_timeout_seconds() -> float:
    """读取完整评测单题的总时限，覆盖多次模型调用的累计等待。"""
    return _positive_seconds("EVALUATION_CASE_TIMEOUT_SECONDS", "300")
