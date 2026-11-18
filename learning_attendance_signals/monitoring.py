"""监测：每门课程使用自己的可比历史，按轮次检验出勤是否恶化。

设计要点：
- 基线只取本课程、同 cohort、非停课的自有历史，绝不借用其他课程的稳定程度。
- 新开课（自有历史不足 startup_rounds）进入 startup 模式：只给观察信号，不确认异常。
- 单轮检验用二项下尾概率（小 n 精确计算，大 n 正态近似）。
- 同一轮同时观察多门课程，用 Benjamini-Hochberg 控制错误发现率，
  避免"每次单独看起来显著的波动都被当成新的确定异常"。
- 信号需连续确认轮数（默认 2）才升级为确定异常；单轮波动仅保留为观察中。
"""
from __future__ import annotations

import math

from .models import Course, CourseFact, EvidenceGrade, Issue, IssueState, Signal

DEFAULT_Q = 0.15
MIN_TESTABLE_SLOTS = 8
CONFIRM_STREAK = 2
MIN_OBSERVABLE_DROP = 0.05
EXACT_BINOMIAL_LIMIT = 30
BASELINE_EPS = 0.01
MU_FLOOR, MU_CAP = 0.01, 0.99


def binomial_lower_tail(k, n, p):
    """P(X <= k)，X~Binomial(n, p)。小 n 精确计算，大 n 连续性修正正态近似。"""
    if n == 0:
        return 1.0
    if n <= EXACT_BINOMIAL_LIMIT:
        total = 0.0
        term = (1.0 - p) ** n
        for i in range(0, min(k, n) + 1):
            if i > 0:
                term *= (n - i + 1) / i * p / (1.0 - p)
            total += term
        return min(1.0, max(0.0, total))
    z = (k + 0.5 - n * p) / math.sqrt(max(n * p * (1.0 - p), 1e-12))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def benjamini_hochberg(p_values, q):
    """返回与输入等长的 bool 列表，标记通过 BH 校正的位置。"""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    accepted = [False] * m
    last_accepted = -1
    for rank, index in enumerate(order, start=1):
        if p_values[index] <= q * rank / m:
            last_accepted = rank - 1
    for index in order[: last_accepted + 1]:
        accepted[index] = True
    return accepted


def grade_evidence(effect_drop, adjusted_p, affected, expected_slots):
    if effect_drop is None:
        return EvidenceGrade.NONE
    strong_drop = effect_drop >= 0.20
    medium_drop = effect_drop >= 0.10
    if adjusted_p is not None and adjusted_p < 0.05 and strong_drop and len(affected) >= 3:
        return EvidenceGrade.STRONG
    if adjusted_p is not None and adjusted_p < 0.10 and medium_drop:
        return EvidenceGrade.MEDIUM
    if effect_drop >= MIN_OBSERVABLE_DROP:
        return EvidenceGrade.WEAK
    return EvidenceGrade.NONE


class MonitoringService:
    def __init__(self, courses, facts, signal_history=None, q=DEFAULT_Q,
                 confirm_streak=CONFIRM_STREAK, min_slots=MIN_TESTABLE_SLOTS):
        self.courses = {c.course_id: c for c in courses}
        self.facts = facts  # {(course_id, period): CourseFact}
        # 历史信号用于识别"已被标记/抑制的轮次"，避免把恶化轮纳入可比基线
        self.signal_history = signal_history or {}
        self.q = q
        self.confirm_streak = confirm_streak
        self.min_slots = min_slots

    # ---- 基线 ----------------------------------------------------------
    def baseline_for(self, course_id, period):
        course = self.courses[course_id]
        history = []
        for (cid, p), fact in self.facts.items():
            if cid != course_id or p >= period or fact.suspended or fact.rate is None:
                continue
            prior = self.signal_history.get((cid, p))
            # 已报警轮、被人工说明抑制轮都不是"稳定"证据，不进入可比基线
            if prior is not None and (prior.flagged or prior.suppressed):
                continue
            history.append((p, fact))
        history.sort()
        own = [fact for _, fact in history]
        startup = len(own) < max(1, course.startup_rounds)
        if not own:
            # 恶化已持续多轮时，锚定最早可用的自有历史（开课时的稳定水平）
            fallback = sorted(
                (fact for (cid, p), fact in self.facts.items()
                 if cid == course_id and p < period and not fact.suspended and fact.rate is not None),
                key=lambda f: f.period,
            )
            if not fallback:
                return None, startup
            own = fallback[:1]
        # 以应到人次数加权的自有基线
        weighted = sum(f.rate * f.expected_slots for f in own)
        weight = sum(f.expected_slots for f in own)
        return min(MU_CAP, max(MU_FLOOR, weighted / weight)), startup

    # ---- 单轮评估 ------------------------------------------------------
    def evaluate_period(self, period, annotations_by_course=None):
        """返回该轮全部课程的 Signal 列表（含未报警课程，便于追查"为何没报"）。"""
        annotations_by_course = annotations_by_course or {}
        raw = []
        for course in self.courses.values():
            fact = self.facts.get((course.course_id, period))
            if fact is None:
                continue
            signal = self._evaluate_course(course, fact)
            raw.append(signal)

        testable = [s for s in raw if s.p_value is not None]
        accepted = benjamini_hochberg([s.p_value for s in testable], self.q)
        for signal, ok in zip(testable, accepted):
            signal.adjusted_p = signal.p_value  # 默认值，下面替换为真实 BH 调整值
            signal.flagged = ok
        adjusted_values = _bh_adjusted([s.p_value for s in testable])
        for signal, adj in zip(testable, adjusted_values):
            signal.adjusted_p = adj
            if signal.flagged:
                signal.grade = grade_evidence(
                    signal.effect_drop, adj, signal.affected_students, signal.expected_slots
                )

        # 权威停课：监测挂起；人工说明：仅抑制所标注的这一轮
        for signal in raw:
            if signal.detail.get("suspended"):
                signal.reasons.append("本轮无应到课次（停课/调课），监测挂起")
                continue
            annotation = annotations_by_course.get(signal.course_id)
            if annotation is not None and annotation.period == period and signal.flagged:
                signal.suppressed = True
                signal.suppression_reason = annotation.reason
                signal.reasons.append(f"人工说明（仅本轮有效）：{annotation.reason}")
        return raw

    def _evaluate_course(self, course, fact: CourseFact) -> Signal:
        signal = Signal(
            course_id=course.course_id,
            period=fact.period,
            rate=fact.rate,
            baseline=None,
            expected_slots=fact.expected_slots,
            affected_students=fact.affected_students,
        )
        if fact.suspended:
            signal.detail["suspended"] = True
            signal.reasons.append("无应到课次")
            return signal

        baseline, startup = self.baseline_for(course.course_id, fact.period)
        signal.baseline = baseline
        signal.startup = startup

        if fact.expected_slots < self.min_slots:
            signal.reasons.append(f"应到人次数 {fact.expected_slots} 低于检验下限 {self.min_slots}")
            return signal
        if baseline is None:
            signal.reasons.append(
                f"新开课缺少自有历史基线（需 {course.startup_rounds} 轮），仅观察不确认，"
                "不套用其他课程的稳定程度")
            return signal

        p_value = binomial_lower_tail(fact.present_slots, fact.expected_slots, baseline)
        signal.p_value = p_value
        signal.effect_drop = baseline - fact.rate
        signal.detail["present_slots"] = fact.present_slots
        signal.detail["absent_slots"] = fact.absent_slots
        signal.detail["raw_p"] = p_value

        if startup:
            signal.reasons.append(
                f"新开课（自有历史不足 {course.startup_rounds} 轮），不套用其他课程的稳定程度"
            )
            if fact.rate < baseline - MIN_OBSERVABLE_DROP:
                signal.grade = EvidenceGrade.WEAK
        elif fact.rate >= baseline - MIN_OBSERVABLE_DROP:
            signal.reasons.append("降幅低于可观察阈值，视为正常波动")
        return signal

    # ---- 持续性确认与问题状态机 ----------------------------------------
    def reconcile_issues(self, signals, existing_issues):
        """把本轮信号与已有问题合并，产出本轮问题视图与状态变更事件。"""
        issues = {i.issue_id: i for i in existing_issues}
        by_course = {i.course_id: i for i in existing_issues}
        changes = []
        for signal in signals:
            if signal.detail.get("suspended") or signal.suppressed:
                issue = by_course.get(signal.course_id)
                if issue is not None and issue.state not in (
                    IssueState.SUSPENDED, IssueState.EXPLAINED
                ):
                    if signal.suppressed:
                        issue.state = IssueState.EXPLAINED
                        changes.append((issue, "explained", signal.suppression_reason))
                continue

            confirmed = (
                signal.flagged
                and not signal.startup
                and signal.grade in (EvidenceGrade.MEDIUM, EvidenceGrade.STRONG)
            )
            issue = by_course.get(signal.course_id)
            if confirmed:
                if issue is None:
                    # 第一条信号只是观察中，必须再持续 confirm_streak-1 轮才确认
                    issue = Issue(
                        issue_id=f"ISSUE-{signal.course_id}",
                        course_id=signal.course_id,
                        opened_period=signal.period,
                        state=IssueState.WATCHING,
                        streak=1,
                        last_period=signal.period,
                    )
                    issues[issue.issue_id] = issue
                    by_course[signal.course_id] = issue
                    changes.append((issue, "watching", "首轮统计信号，进入观察"))
                    continue
                issue.streak += 1
                issue.last_period = signal.period
                if issue.state in (IssueState.CONTACTED,
                                   IssueState.CONTACTED_NO_IMPROVEMENT):
                    issue.state = IssueState.CONTACTED_NO_IMPROVEMENT
                    changes.append((issue, "no_improvement", "联系后新一轮证据仍恶化"))
                elif issue.state in (IssueState.WATCHING, IssueState.OPEN_NEW,
                                     IssueState.OPEN_PERSISTENT):
                    if issue.streak >= self.confirm_streak:
                        if issue.streak > self.confirm_streak:
                            issue.state = IssueState.OPEN_PERSISTENT
                            changes.append((issue, "persistent",
                                            f"连续 {issue.streak} 轮确认且尚未处理完"))
                        else:
                            issue.state = IssueState.OPEN_NEW
                            changes.append((issue, "confirmed",
                                            f"连续 {issue.streak} 轮证据确认"))
                    else:
                        changes.append((issue, "streak", f"连续计数 {issue.streak}"))
            else:
                if issue is not None and issue.state not in (
                    IssueState.RESOLVED, IssueState.SUSPENDED
                ):
                    # 恢复判定：回到可比基线附近
                    if signal.rate is not None and signal.baseline is not None \
                            and signal.rate >= signal.baseline - MIN_OBSERVABLE_DROP:
                        issue.state = IssueState.RESOLVED
                        issue.streak = 0
                        changes.append((issue, "resolved", "出勤回到可比基线"))
                    elif issue.state in (IssueState.OPEN_NEW, IssueState.OPEN_PERSISTENT):
                        changes.append((issue, "not_confirmed", "本轮未通过多重比较/持续性门槛"))
        return list(issues.values()), changes


def _bh_adjusted(p_values):
    """BH 调整后的 p 值（单调化），用于展示与证据分级。"""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        index = order[rank - 1]
        running = min(running, p_values[index] * m / rank)
        adjusted[index] = min(1.0, running)
    return adjusted
