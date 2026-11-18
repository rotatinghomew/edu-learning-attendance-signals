"""兼容层：早期 CSV 记录读取，实际实现见 legacy.read_records。"""
from .legacy import read_records

__all__ = ["read_records"]
