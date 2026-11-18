"""逐轮编排：事实 → 监测 → 解释 → 分解 → 联系排序 → 审计留痕。

每个轮次的处理是幂等可重放的：输入同一批主数据与历史状态，产出相同的
信号、问题状态与审计事件。原始出勤账在整个过程中只读。
"""
from __future__ import annotations

from .audit import AuditLog
from .calendar import build_facts
from .decomposition import affected_headcount, decompose
from .explanations import ExplanationService
from .models import Issue
from .monitoring import MonitoringService
from .operations import apply_contacts, plan_contacts


class Pipeline:
    def __init__(self, repository, contact_budget=2, audit=None):
        self.repo = repository
        self.contact_budget = contact_budget
        self.audit = audit or AuditLog()

    # ---- 事实层（只算一次，后续轮次复用）----
    def refresh_facts(self):
        facts = build_facts(
            self.repo.list_courses(),
            self.repo.list_sessions(),
            self.repo.list_enrollments(),
            self.repo.list_attendance(),
        )
        for fact in facts.values():
            self.repo.save_fact(fact)
            for quarantined in fact.quarantined:
                self.audit.record(
                    fact.period, fact.course_id, "record_quarantined",
                    quarantined.reason,
                    {"student_id": quarantined.record.student_id,
                     "session_date": quarantined.record.session_date})
        return facts

    def run_round(self, period, facts=None):
        repo = self.repo
        facts = facts or self._facts_map()
        explanations = ExplanationService(repo.list_annotations())
        history = {(s.course_id, s.period): s for s in repo.list_signals()
                   if s.period < period}
        monitor = MonitoringService(repo.list_courses(), facts,
                                    signal_history=history)

        signals = monitor.evaluate_period(
            period,
            annotations_by_course=explanations.annotations_by_course_for_period(period),
        )
        for signal in signals:
            repo.save_signal(signal)
            self._audit_signal(signal, explanations)

        issues = repo.list_issues()
        suspension_changes = explanations.apply_suspensions(signals, issues)
        for issue, kind, reason in suspension_changes:
            repo.save_issue(issue)
            self.audit.record(period, issue.course_id, "issue_state", reason,
                              {"transition": kind, "state": issue.state.value})

        issues, changes = monitor.reconcile_issues(signals, issues)
        for issue, kind, reason in changes:
            repo.save_issue(issue)
            self.audit.record(period, issue.course_id, "issue_state", reason,
                              {"transition": kind, "state": issue.state.value})

        decomposition = None
        if period > 1:
            decomposition = decompose(facts, period - 1, period)
            if decomposition is not None:
                headcount = affected_headcount(facts, signals, period)
                self.audit.record(
                    period, None, "overall_decomposition",
                    "总体出勤率变化已拆分为内部/构成/新开课效应",
                    {k: v for k, v in decomposition.items()
                     if k not in ("courses", "new_courses", "removed_courses")}
                    | {"affected_headcount": headcount})

        handled = self._already_handled_students(period)
        attempts, skipped, covered = plan_contacts(
            issues, signals, self.contact_budget, already_handled_students=handled)
        for attempt in attempts:
            repo.add_contact(attempt)
            self.audit.record(
                period, attempt.course_id, "contact_planned",
                f"证据 {attempt.evidence_grade.value}，本轮唯一覆盖 {len(attempt.students)} 人",
                {"issue_id": attempt.issue_id, "score": attempt.score,
                 "students": list(attempt.students),
                 "score_breakdown": attempt.score_breakdown})
        for course_id, reason in skipped:
            self.audit.record(period, course_id, "contact_skipped", reason, {})
        apply_contacts(issues, attempts)
        for issue in issues:
            repo.save_issue(issue)

        # 固化本轮结束时的状态快照，避免后续轮次原地修改对象影响历史结果
        issue_snapshot = [
            Issue(issue.issue_id, issue.course_id, issue.opened_period,
                  issue.state, issue.streak, issue.last_period)
            for issue in repo.list_issues()
        ]
        return {
            "period": period,
            "signals": signals,
            "issues": issue_snapshot,
            "decomposition": decomposition,
            "attempts": attempts,
            "skipped": skipped,
        }

    def run_all(self):
        facts = self.refresh_facts()
        periods = sorted({p for _, p in facts})
        results = []
        for period in periods:
            results.append(self.run_round(period, facts=facts))
        return results

    # ---- 内部工具 ----
    def _facts_map(self):
        return {(f.course_id, f.period): f for f in self.repo.list_facts()}

    def _already_handled_students(self, period):
        handled = set()
        for attempt in self.repo.list_contacts():
            handled.update(attempt.students)
        return handled

    def _audit_signal(self, signal, explanations):
        explanation = explanations.describe(signal)
        if signal.detail.get("suspended"):
            event_type, reason = "monitor_suspended", "本轮无应到课次，不参与出勤检验"
        elif signal.suppressed:
            event_type = "monitor_suppressed"
            reason = f"人工说明仅本轮有效：{signal.suppression_reason}"
        elif signal.flagged:
            event_type = "monitor_flagged"
            reason = (f"统计信号：率 {signal.rate:.3f} vs 自有基线 {signal.baseline:.3f}，"
                      f"BH 调整 p={signal.adjusted_p:.4f}，证据 {signal.grade.value}")
        else:
            event_type = "monitor_clear"
            why = "；".join(signal.reasons) if signal.reasons else "无显著恶化"
            if signal.p_value is not None and not signal.flagged:
                why = (f"未通过 BH 多重比较校正（原始 p={signal.p_value:.4f}，"
                       f"调整后 p={signal.adjusted_p:.4f}）")
            reason = "未触发提醒：" + why
        self.audit.record(
            signal.period, signal.course_id, event_type, reason,
            {"rate": signal.rate, "baseline": signal.baseline,
             "p_value": signal.p_value, "adjusted_p": signal.adjusted_p,
             "effect_drop": signal.effect_drop, "grade": signal.grade.value,
             "startup": signal.startup, "flagged": signal.flagged,
             "affected_students": list(signal.affected_students),
             "explanation": explanation})
