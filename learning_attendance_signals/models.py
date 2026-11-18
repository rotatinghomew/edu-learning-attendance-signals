"""领域模型：课程、学员、课表、出勤、问题与处置状态。

口径约定：
- 所有"率"的分母只包含实际应上的课次 × 当日在册学员。
- 原始出勤记录不可变；任何派生/校正值单独存放并标注 basis。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SessionKind(str, Enum):
    SCHEDULED = "scheduled"            # 正常安排
    RESCHEDULED_OUT = "rescheduled_out"  # 调走：本日不应到
    MAKEUP = "makeup"                  # 补回的课：本日应到
    CANCELLED = "cancelled"            # 停课：本日不应到


class IssueState(str, Enum):
    WATCHING = "观察中"                       # 有信号但未确认
    OPEN_NEW = "待联系"                       # 新确认、尚未联系
    OPEN_PERSISTENT = "持续未联系"            # 多轮未联系上的持续问题
    CONTACTED = "已联系"                      # 本轮已联系，等待后续证据
    CONTACTED_NO_IMPROVEMENT = "联系后无改善"  # 联系后证据仍恶化
    EXPLAINED = "已解释"                      # 人工说明（仅对所标注事件有效）
    SUSPENDED = "已解释停课"                  # 权威停课记录，监测挂起
    RESOLVED = "已恢复"                       # 证据回到可比基线


class EvidenceGrade(str, Enum):
    STRONG = "强"
    MEDIUM = "中"
    WEAK = "弱"
    NONE = "无"


@dataclass(frozen=True)
class Course:
    course_id: str
    name: str
    category: str
    cohort: str
    startup_rounds: int = 2  # 进入稳定监测前需要的自有历史轮数


@dataclass(frozen=True)
class Student:
    student_id: str
    name: str


@dataclass(frozen=True)
class Enrollment:
    student_id: str
    course_id: str
    effective_from: str  # ISO 日期，含当日
    effective_to: Optional[str] = None  # ISO 日期，含当日；None 表示长期有效

    def covers(self, day: str) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True)
class ScheduledSession:
    course_id: str
    session_date: str
    period: int
    kind: SessionKind
    note: str = ""


@dataclass(frozen=True)
class AttendanceRecord:
    course_id: str
    session_date: str
    student_id: str
    present: bool
    load_id: str = "seed"


@dataclass(frozen=True)
class Annotation:
    """人工说明。只对精确匹配的 (课程, 轮次) 生效，不会顺延到后续真实下降。"""
    annotation_id: str
    course_id: str
    period: int
    reason: str
    author: str


@dataclass
class QuarantinedRecord:
    record: AttendanceRecord
    reason: str


@dataclass
class CourseFact:
    """单课程单轮次的出勤事实（原始口径）。"""
    course_id: str
    period: int
    expected_sessions: int
    expected_slots: int = 0          # 应到人次数 = Σ(课次 × 当日在册人数)
    present_slots: int = 0
    absent_slots: int = 0
    expected_students: tuple[str, ...] = ()
    affected_students: tuple[str, ...] = ()  # 本轮至少缺席一次的在册学员
    quarantined: list[QuarantinedRecord] = field(default_factory=list)

    @property
    def suspended(self) -> bool:
        return self.expected_sessions == 0

    @property
    def rate(self) -> Optional[float]:
        if self.expected_slots == 0:
            return None
        return self.present_slots / self.expected_slots


@dataclass
class Signal:
    course_id: str
    period: int
    rate: Optional[float]
    baseline: Optional[float]
    expected_slots: int
    affected_students: tuple[str, ...]
    p_value: Optional[float] = None
    adjusted_p: Optional[float] = None
    effect_drop: Optional[float] = None
    grade: EvidenceGrade = EvidenceGrade.NONE
    flagged: bool = False          # 经多重比较校正后的本轮统计信号
    startup: bool = False
    suppressed: bool = False       # 被人工说明抑制（仅本轮）
    suppression_reason: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class Issue:
    issue_id: str
    course_id: str
    opened_period: int
    state: IssueState
    streak: int = 0
    last_period: int = 0


@dataclass
class ContactAttempt:
    issue_id: str
    course_id: str
    period: int
    students: tuple[str, ...]
    evidence_grade: EvidenceGrade
    score: float
    score_breakdown: dict


@dataclass
class AuditEvent:
    seq: Optional[int]
    period: Optional[int]
    course_id: Optional[str]
    event_type: str
    reason: str
    details: dict
