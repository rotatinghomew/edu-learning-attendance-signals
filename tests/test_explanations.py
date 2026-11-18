"""人工说明与权威改课记录的区别测试。"""
import unittest

from learning_attendance_signals.explanations import ExplanationService
from learning_attendance_signals.models import (
    Annotation,
    Issue,
    IssueState,
    Signal,
)


def signal(course_id, period, flagged=True, suppressed=False, suspended=False):
    return Signal(
        course_id=course_id, period=period, rate=0.5, baseline=0.9,
        expected_slots=20, affected_students=("S1",), flagged=flagged,
        suppressed=suppressed, suppression_reason="参展" if suppressed else None,
        detail={"suspended": suspended},
    )


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.service = ExplanationService([
            Annotation("A1", "C5", 5, "作品展参展缺课", "王老师"),
        ])

    def test_annotation_is_scoped_to_exact_period_and_does_not_carry_forward(self):
        self.assertIsNotNone(self.service.annotation_for("C5", 5))
        # 下一轮真实下降不能拿上一轮的说明免责
        self.assertIsNone(self.service.annotation_for("C5", 6))
        self.assertIsNone(self.service.annotation_for("C5", 4))

    def test_duplicate_annotation_for_same_period_rejected(self):
        with self.assertRaises(ValueError):
            ExplanationService([
                Annotation("A1", "C5", 5, "理由一", "甲"),
                Annotation("A2", "C5", 5, "理由二", "乙"),
            ])

    def test_describe_distinguishes_manual_from_authoritative(self):
        manual = self.service.describe(signal("C5", 5, suppressed=True))
        self.assertEqual(manual["kind"], "manual_annotation")
        authoritative = self.service.describe(signal("C3", 5, suspended=True))
        self.assertEqual(authoritative["kind"], "authoritative_schedule")


class SuspensionTests(unittest.TestCase):
    def test_authoritative_suspension_pauses_issue_and_resume_rebuilds_baseline(self):
        service = ExplanationService()
        issue = Issue("ISSUE-C3", "C3", 2, IssueState.OPEN_NEW, streak=2, last_period=4)
        changes = service.apply_suspensions([signal("C3", 5, suspended=True)], [issue])
        self.assertEqual(issue.state, IssueState.SUSPENDED)
        self.assertEqual(changes[0][1], "suspended")

        # 恢复：回到观察中、连续计数清零，等待新一轮证据重新确认
        changes = service.apply_suspensions([signal("C3", 6, flagged=False)], [issue])
        self.assertEqual(issue.state, IssueState.WATCHING)
        self.assertEqual(issue.streak, 0)
        self.assertEqual(changes[0][1], "resumed")

    def test_manual_annotation_never_moves_issue_to_suspended(self):
        service = ExplanationService([Annotation("A1", "C5", 5, "参展", "王老师")])
        issue = Issue("ISSUE-C5", "C5", 3, IssueState.WATCHING, streak=1, last_period=4)
        # 只有权威停课才能 SUSPENDED；人工抑制不改问题的停课状态
        service.apply_suspensions([signal("C5", 5, suspended=True, flagged=False)], [issue])
        self.assertEqual(issue.state, IssueState.SUSPENDED)  # 这里是 suspended=True 的权威情形
        issue.state = IssueState.WATCHING
        service.apply_suspensions([signal("C5", 5, suppressed=True, flagged=True)], [issue])
        self.assertEqual(issue.state, IssueState.WATCHING)  # 人工说明不产生停课迁移


if __name__ == "__main__":
    unittest.main()
