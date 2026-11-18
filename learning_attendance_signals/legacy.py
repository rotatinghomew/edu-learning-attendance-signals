"""旧版通用 CSV 汇总流程的兼容层。

新业务模型见 calendar/monitoring/operations/pipeline；本模块仅保留
原型早期 observations.csv 的读取-汇总-审阅流程，保证既有示例与测试不中断。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


def read_records(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return []
    required = {"period", "group", "count", "value"}
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"缺少字段: {', '.join(sorted(missing))}")
    return rows


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["period"], row["group"])].append(row)
    result = []
    for (period, group), members in sorted(grouped.items()):
        count = sum(int(row["count"]) for row in members)
        values = [float(row["value"]) for row in members]
        result.append({"period": period, "group": group, "count": count,
                       "mean": sum(values) / len(values) if values else None})
    return result


def review(summary, minimum_count=1):
    return [{**row, "review": "可查看" if row["count"] >= minimum_count else "资料不足"}
            for row in summary]
