"""监测统计与持续性确认测试。"""
import unittest

from learning_attendance_signals.models import (
    Course,
    CourseFact,
    EvidenceGrade,
    IssueState,
    Signal,
)
from learning_attendance_signals.monitoring import (
    benjamini_hochberg,
    binomial_lower_tail,
    grade_evidence,
    MonitoringService,
)


def fact(course_id, period, slots, present, affected=()):
    return CourseFact(course_id, period, expected_sessions=1,
                      expected_slots=slots, present_slots=present,
                      absent_slots=slots - present,
                      affected_students=tuple(affected))


class StatsTests(unittest.TestCase):
    def test_binomial_tail_matches_known_values(self):
        # 抛硬币 10 次，正面 <=2 次的精确概率 = (1+10+45)/1024
        self.assertAlmostEqual(binomial_lower_tail(2, 10, 0.5), 56 / 1024, places=6)
        self.assertAlmostEqual(binomial_lower_tail(0, 5, 0.2), 0.8 ** 5, places=6)
        self.assertEqual(binomial_lower_tail(10, 10, 0.5), 1.0)

    def test_normal_approximation_is_close_for_large_n(self):
        exact = binomial_lower_tail(40, 100, 0.5)
        # 大 n 走近似分支，应落在精确值附近（z≈-1.89 → 约 0.029）
        self.assertLess(exact, 0.06)
        self.assertGreater(exact, 0.01)

    def test_bh_controls_false_discoveries(self):
        # 10 个检验，只有一个极小 p；q=0.15 时仅最小者通过
        pvals = [0.001] + [0.55] * 9
        accepted = benjamini_hochberg(pvals, 0.15)
        self.assertEqual(sum(accepted), 1)
        self.assertTrue(accepted[0])

    def test_bh_rejects_isolated_marginal_blip_among_many_courses(self):
        # 单看 p=0.04 显著，但放在 20 门课里不应直接升级为确定异常
        pvals = [0.04] + [0.6] * 19
        accepted = benjamini_hochberg(pvals, 0.10)
        self.assertFalse(accepted[0])

    def test_grade_strength_combines_drop_significance_and_headcount(self):
        self.assertEqual(grade_evidence(0.25, 0.01, ("a", "b", "c"), 20),
                         EvidenceGrade.STRONG)
        self.assertEqual(grade_evidence(0.12, 0.08, ("a",), 20), EvidenceGrade.MEDIUM)
        self.assertEqual(grade_evidence(0.06, 0.01, ("a", "b", "c"), 20),
                         EvidenceGrade.WEAK)
        self.assertEqual(grade_evidence(0.01, 0.01, ("a",), 20), EvidenceGrade.NONE)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.courses = [Course("C1", "书法", "书画", "2025秋", startup_rounds=2)]
        self.facts = {
            ("C1", 1): fact("C1", 1, 20, 18),  # 0.9
            ("C1", 2): fact("C1", 2, 20, 18),  # 0.9
            ("C1", 3): fact("C1", 3, 20, 10),  # 0.5（恶化轮）
        }

    def test_baseline_uses_only_own_weighted_history(self):
        monitor = MonitoringService(self.courses, self.facts)
        baseline, startup = monitor.baseline_for("C1", 3)
        self.assertAlmostEqual(baseline, 0.9)
        self.assertFalse(startup)

    def test_flagged_and_suppressed_rounds_excluded_from_baseline(self):
        history = {
            ("C1", 3): Signal(course_id="C1", period=3, rate=0.5, baseline=0.9,
                              expected_slots=20, affected_students=(), flagged=True),
        }
        facts = dict(self.facts)
        facts[("C1", 4)] = fact("C1", 4, 20, 9)
        monitor = MonitoringService(self.courses, facts, signal_history=history)
        baseline, _ = monitor.baseline_for("C1", 4)
        self.assertAlmostEqual(baseline, 0.9)  # 锚定稳定历史，不被恶化轮稀释

    def test_new_course_without_history_is_startup_and_never_confirmed(self):
        courses = [Course("C4", "智能生活", "科技", "2026春", startup_rounds=2)]
        facts = {("C4", 1): fact("C4", 1, 26, 10)}
        monitor = MonitoringService(courses, facts)
        signals = monitor.evaluate_period(1)
        signal = signals[0]
        self.assertTrue(signal.startup)
        self.assertFalse(signal.flagged)
        self.assertIn("新开课", " ".join(signal.reasons))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.courses = [Course("C1", "书法", "书画", "2025秋", startup_rounds=1)]
        # 一轮稳定 + 三轮持续恶化
        self.facts = {
            ("C1", 1): fact("C1", 1, 30, 27, tuple(f"S{i}" for i in range(3))),
            ("C1", 2): fact("C1", 2, 30, 12, tuple(f"S{i}" for i in range(12))),
            ("C1", 3): fact("C1", 3, 30, 11, tuple(f"S{i}" for i in range(13))),
            ("C1", 4): fact("C1", 4, 30, 10, tuple(f"S{i}" for i in range(14))),
        }
        self.monitor = MonitoringService(self.courses, self.facts)

    def test_single_blip_stays_watching_and_two_streak_confirms(self):
        issues = []
        s2 = self.monitor.evaluate_period(2)
        self.assertTrue(s2[0].flagged)
        issues, changes = self.monitor.reconcile_issues(s2, issues)
        self.assertEqual(issues[0].state, IssueState.WATCHING)
        self.assertEqual(issues[0].streak, 1)

        s3 = self.monitor.evaluate_period(3)
        issues, changes = self.monitor.reconcile_issues(s3, issues)
        self.assertEqual(issues[0].state, IssueState.OPEN_NEW)
        kinds = [kind for _, kind, _ in changes]
        self.assertIn("confirmed", kinds)

        s4 = self.monitor.evaluate_period(4)
        issues, changes = self.monitor.reconcile_issues(s4, issues)
        self.assertEqual(issues[0].state, IssueState.OPEN_PERSISTENT)

    def test_recovery_returns_to_resolved_and_resets_streak(self):
        issues = []
        for period in (2, 3):
            signals = self.monitor.evaluate_period(period)
            issues, _ = self.monitor.reconcile_issues(signals, issues)
        self.assertEqual(issues[0].state, IssueState.OPEN_NEW)
        # 下一轮恢复到基线附近
        self.facts[("C1", 5)] = fact("C1", 5, 30, 27)
        signals = self.monitor.evaluate_period(5)
        issues, changes = self.monitor.reconcile_issues(signals, issues)
        self.assertEqual(issues[0].state, IssueState.RESOLVED)
        self.assertEqual(issues[0].streak, 0)


if __name__ == "__main__":
    unittest.main()
