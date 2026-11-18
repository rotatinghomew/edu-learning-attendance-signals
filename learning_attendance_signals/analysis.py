"""兼容层：早期通用汇总，实际实现见 legacy.summarize。

新的出勤事实口径见 calendar.build_facts。
"""
from .legacy import summarize

__all__ = ["summarize"]
