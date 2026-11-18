"""有限联系能力测试：预算、跨班去重、证据×覆盖排序、状态机。"""
import unittest

from learning_attendance_signals.models import (
    EvidenceGrade,
    Issue,
    IssueState,
    Signal,
)
from learning_attendance_signals.operations import (
    apply_contacts,
    plan_contacts,
    rank_issues,
)


def signal(course_id, grade, affected):
    return Signal(course_id=course_id, period=6, rate=0.5, baseline=0.9,
                  expected_slots=20, affected_students=tuple(affected),
                  grade=grade, flagged=True)


def issue(course_id, state):
    return Issue(f"ISSUE-{course_id}", course_id, 3, state, streak=2, last_period=5)


class ContactPlanningTests(unittest.TestCase):
    def test_budget_caps_number_of_classes_contacted(self):
        issues = [
            issue("A", IssueState.OPEN_NEW),
            issue("B", IssueState.OPEN_NEW),
            issue("C", IssueState.OPEN_NEW),
        ]
        signals = [
            signal("A", EvidenceGrade.STRONG, ["S1", "S2"]),
            signal("B", EvidenceGrade.MEDIUM, ["S3"]),
            signal("C", EvidenceGrade.WEAK, ["S4"]),
        ]
        attempts, skipped, _ = plan_contacts(issues, signals, budget=2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual([a.course_id for a in attempts], ["A", "B"])
        self.assertTrue(any(course == "C" for course, _ in skipped))

    def test_cross_class_student_consumes_budget_only_once(self):
        issues = [
            issue("A", IssueState.OPEN_NEW),
            issue("B", IssueState.OPEN_NEW),
        ]
        signals = [
            signal("A", EvidenceGrade.STRONG, ["S1", "S2"]),
            # B 的学员全部已被 A 覆盖
            signal("B", EvidenceGrade.STRONG, ["S1", "S2"]),
        ]
        attempts, skipped, covered = plan_contacts(issues, signals, budget=5)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].course_id, "A")
        self.assertEqual(skipped[0][0], "B")
        self.assertEqual(covered, {"S1", "S2"})

    def test_ranking_combines_evidence_and_unique_coverage(self):
        issues = [
            issue("WEAK_WIDE", IssueState.OPEN_NEW),
            issue("STRONG_NARROW", IssueState.OPEN_NEW),
        ]
        signals = [
            signal("WEAK_WIDE", EvidenceGrade.WEAK, [f"S{i}" for i in range(8)]),
            signal("STRONG_NARROW", EvidenceGrade.STRONG, ["S9"]),
        ]
        ranked = rank_issues(issues, signals)
        # 弱证据但一次联系覆盖 8 人：1.0 + 0.5*8 = 5.0；强证据 1 人：3.0 + 0.5 = 3.5
        self.assertEqual(ranked[0].issue.course_id, "WEAK_WIDE")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_already_handled_students_are_not_recontacted(self):
        issues = [issue("A", IssueState.OPEN_NEW)]
        signals = [signal("A", EvidenceGrade.STRONG, ["S1", "S2", "S3"])]
        attempts, _, covered = plan_contacts(
            issues, signals, budget=2, already_handled_students={"S1", "S2"})
        self.assertEqual(attempts[0].students, ("S3",))
        self.assertEqual(covered, {"S1", "S2", "S3"})

    def test_actionable_states_are_separate_from_explained_and_suspended(self):
        issues = [
            issue("NEW", IssueState.OPEN_NEW),
            issue("PERSIST", IssueState.OPEN_PERSISTENT),
            issue("NOIMP", IssueState.CONTACTED_NO_IMPROVEMENT),
            issue("EXPLAINED", IssueState.EXPLAINED),
            issue("SUSP", IssueState.SUSPENDED),
            issue("RESOLVED", IssueState.RESOLVED),
        ]
        signals = [signal(i.course_id, EvidenceGrade.STRONG, ["S1"]) for i in issues]
        ranked = rank_issues(issues, signals)
        actionable = {item.issue.course_id for item in ranked}
        self.assertEqual(actionable, {"NEW", "PERSIST", "NOIMP"})

    def test_contact_only_changes_state_not_attendance_numbers(self):
        target = issue("A", IssueState.OPEN_NEW)
        signals = [signal("A", EvidenceGrade.STRONG, ["S1"])]
        attempts, _, _ = plan_contacts([target], signals, budget=1)
        changes = apply_contacts([target], attempts)
        self.assertEqual(target.state, IssueState.CONTACTED)
        self.assertEqual(changes[0][1], "contacted")
        self.assertEqual(signals[0].rate, 0.5)  # 证据数值原封不动


if __name__ == "__main__":
    unittest.main()
