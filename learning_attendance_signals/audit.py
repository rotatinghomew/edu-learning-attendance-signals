"""可追查事件流：每个监测结论、抑制、状态迁移与联系决策都留痕。

事件回答三类问题：
1. 提醒为什么出现 / 未出现（monitor_* 事件，未报警课程同样记录原因）；
2. 为什么排在当前联系批次（contact_planned 事件，含分数拆解与去重人数）；
3. 后续证据为何改变处置理由（issue_state 事件，含前后状态与证据）。
"""
from __future__ import annotations

from dataclasses import asdict

from .models import AuditEvent


class AuditLog:
    def __init__(self):
        self._events = []

    def record(self, period, course_id, event_type, reason, details=None):
        event = AuditEvent(
            seq=len(self._events) + 1,
            period=period,
            course_id=course_id,
            event_type=event_type,
            reason=reason,
            details=dict(details or {}),
        )
        self._events.append(event)
        return event

    def events(self, course_id=None, period=None, event_type=None):
        result = self._events
        if course_id is not None:
            result = [e for e in result if e.course_id == course_id]
        if period is not None:
            result = [e for e in result if e.period == period]
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        return list(result)

    def to_dicts(self):
        return [asdict(event) for event in self._events]
