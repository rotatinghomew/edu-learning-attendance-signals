"""异常解释：人工说明与权威改课记录走两条互不相通的路径。

人工说明（Annotation）：
- 精确绑定 (课程, 轮次)，只抑制该轮的告警升级，原始出勤账与派生率保持不变；
- 不会顺延：下一轮若真实下降，必须重新接受检验，系统不得拿旧说明当免责；
- 被抑制的轮次不进入基线计算（该轮本身不被视为"稳定"证据）。

权威改课记录（ScheduledSession 中的调课/停课/补课）：
- 由课表事实驱动：停课轮没有应到义务，监测挂起并从基线中排除；
- 恢复后以停课之前的自有历史重建可比基线，不与停课轮比较；
- 这是安排层面的事实，不需要人工逐条说明。
"""
from __future__ import annotations

from .models import Annotation, Issue, IssueState


class ExplanationService:
    def __init__(self, annotations=()):
        self._annotations = {}
        for annotation in annotations:
            key = (annotation.course_id, annotation.period)
            if key in self._annotations:
                raise ValueError(f"同一轮次重复人工说明: {key}")
            self._annotations[key] = annotation

    def annotation_for(self, course_id, period):
        """返回该轮的人工说明；其他轮次恒为 None——说明不会顺延。"""
        return self._annotations.get((course_id, period))

    def annotations_by_course_for_period(self, period):
        return {
            course_id: annotation
            for (course_id, p), annotation in self._annotations.items()
            if p == period
        }

    def apply_suspensions(self, signals, issues):
        """权威停课事实把问题置为 SUSPENDED；恢复后由监测重新确认。"""
        changes = []
        by_course = {issue.course_id: issue for issue in issues}
        suspended_courses = {
            signal.course_id for signal in signals if signal.detail.get("suspended")
        }
        for course_id in suspended_courses:
            issue = by_course.get(course_id)
            if issue is not None and issue.state != IssueState.SUSPENDED:
                previous = issue.state
                issue.state = IssueState.SUSPENDED
                changes.append((issue, "suspended",
                                f"权威停课/调课记录：监测挂起（原状态 {previous.value}）"))
        for issue in issues:
            if issue.course_id not in suspended_courses and issue.state == IssueState.SUSPENDED:
                issue.state = IssueState.WATCHING
                issue.streak = 0
                changes.append((issue, "resumed",
                                "课程恢复，按停课前后自有历史重建可比基线"))
        return changes

    def describe(self, signal):
        """给信号生成人类可读的解释条目，明确区分两类来源。"""
        if signal.detail.get("suspended"):
            return {"kind": "authoritative_schedule",
                    "text": "本轮无应到课次（停课或调课），不构成缺席证据"}
        if signal.suppressed:
            return {"kind": "manual_annotation",
                    "text": f"人工说明（仅本轮）：{signal.suppression_reason}"}
        return None
