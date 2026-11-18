"""有限联系能力：一轮只能联系有限班级，按证据强度与去重覆盖排序。

排序依据（贪心）：
  score = 证据权重 + 唯一覆盖奖励（该班尚未处理、且未被本轮其他联系覆盖的学员数）
一个学员跨班报名时，只有第一份包含他的联系占用预算，后续班级不再因他得分；
待联系、持续未联系、已解释停课、联系后无改善在状态机中各自独立、不混为一种。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ContactAttempt,
    EvidenceGrade,
    Issue,
    IssueState,
    Signal,
)

# 旧版通用审阅流程的兼容导出（新业务状态机见本文件下方）
from .legacy import review as review  # noqa: F401  兼容 learning_attendance_signals.operations.review

GRADE_WEIGHT = {
    EvidenceGrade.STRONG: 3.0,
    EvidenceGrade.MEDIUM: 2.0,
    EvidenceGrade.WEAK: 1.0,
    EvidenceGrade.NONE: 0.0,
}
COVERAGE_WEIGHT = 0.5
PERSISTENCE_BONUS = 0.5
NO_IMPROVEMENT_BONUS = 0.25

ACTIONABLE_STATES = {
    IssueState.OPEN_NEW,
    IssueState.OPEN_PERSISTENT,
    IssueState.CONTACTED_NO_IMPROVEMENT,
}


@dataclass
class RankedIssue:
    issue: Issue
    signal: Signal
    unique_students: tuple[str, ...]
    score: float
    breakdown: dict


def _signal_index(signals):
    return {s.course_id: s for s in signals}


def rank_issues(issues, signals, already_handled_students=()):
    """返回按分数降序的可行动问题列表（不做预算截断）。"""
    signals_by_course = _signal_index(signals)
    handled = set(already_handled_students)
    ranked = []
    for issue in issues:
        if issue.state not in ACTIONABLE_STATES:
            continue
        signal = signals_by_course.get(issue.course_id)
        if signal is None:
            continue
        unique = tuple(s for s in signal.affected_students if s not in handled)
        breakdown = {
            "evidence_weight": GRADE_WEIGHT[signal.grade],
            "coverage_weight": COVERAGE_WEIGHT * len(unique),
            "persistence_bonus": PERSISTENCE_BONUS if issue.state == IssueState.OPEN_PERSISTENT else 0.0,
            "no_improvement_bonus": NO_IMPROVEMENT_BONUS
            if issue.state == IssueState.CONTACTED_NO_IMPROVEMENT else 0.0,
            "duplicate_students_skipped": len(signal.affected_students) - len(unique),
        }
        score = sum(breakdown[k] for k in
                    ("evidence_weight", "coverage_weight", "persistence_bonus", "no_improvement_bonus"))
        ranked.append(RankedIssue(issue, signal, unique, score, breakdown))
    ranked.sort(key=lambda item: (-item.score, item.issue.course_id))
    return ranked


def plan_contacts(issues, signals, budget, already_handled_students=()):
    """在 budget 个班级上限内贪心选批，跨班学员只占一次预算。"""
    ranking = rank_issues(issues, signals, already_handled_students)
    handled = set(already_handled_students)
    attempts, skipped = [], []
    for item in ranking:
        if len(attempts) >= budget:
            skipped.append((item.issue.course_id, "本轮联系预算已满"))
            continue
        newly_covered = tuple(s for s in item.signal.affected_students if s not in handled)
        if not newly_covered and item.unique_students:
            # 排名时唯一、被同轮更早的班级先覆盖
            skipped.append((item.issue.course_id, "受影响学员已被同轮其他联系覆盖"))
            continue
        attempt = ContactAttempt(
            issue_id=item.issue.issue_id,
            course_id=item.issue.course_id,
            period=item.signal.period,
            students=newly_covered,
            evidence_grade=item.signal.grade,
            score=item.score,
            score_breakdown=item.breakdown,
        )
        attempts.append(attempt)
        handled.update(newly_covered)
    return attempts, skipped, handled


def apply_contacts(issues, attempts):
    """联系落地：仅状态迁移，不改任何出勤数值。

    已经历过"联系后无改善"的问题保留升级标记，不回退为普通已联系，
    使待联系、持续未联系、已解释停课、无改善在台账中始终可区分。
    """
    by_id = {issue.issue_id: issue for issue in issues}
    changes = []
    for attempt in attempts:
        issue = by_id[attempt.issue_id]
        previous = issue.state
        if previous == IssueState.CONTACTED_NO_IMPROVEMENT:
            new_state = IssueState.CONTACTED_NO_IMPROVEMENT
            note = "重复联系（联系后仍无改善）"
        else:
            new_state = IssueState.CONTACTED
            note = f"联系 {len(attempt.students)} 名学员"
        issue.state = new_state
        changes.append((issue, "contacted",
                        f"{note}（原状态 {previous.value}）"))
    return changes
