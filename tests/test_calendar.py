"""calendar.build_facts 的口径测试：
调课日不算缺席、停课无应到义务、在册资格过滤、原始账隔离。
"""
import unittest

from learning_attendance_signals.calendar import build_facts
from learning_attendance_signals.models import (
    AttendanceRecord,
    Course,
    Enrollment,
    ScheduledSession,
    SessionKind,
)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.course = Course("C1", "书法", "书画", "2025秋", startup_rounds=2)
        self.sessions = [
            ScheduledSession("C1", "2026-03-02", 1, SessionKind.SCHEDULED),
            ScheduledSession("C1", "2026-03-16", 2, SessionKind.RESCHEDULED_OUT, "调走"),
            ScheduledSession("C1", "2026-03-18", 2, SessionKind.MAKEUP, "补课"),
            ScheduledSession("C1", "2026-03-30", 3, SessionKind.CANCELLED, "停课"),
        ]
        self.enrollments = [
            Enrollment("S1", "C1", "2026-01-01"),
            Enrollment("S2", "C1", "2026-01-01", "2026-03-10"),  # R2 起不在册
        ]

    def test_rescheduled_day_has_no_duty_and_missing_records_are_not_absences(self):
        attendance = [
            # R1：S1 到、S2 缺
            AttendanceRecord("C1", "2026-03-02", "S1", True),
            AttendanceRecord("C1", "2026-03-02", "S2", False),
            # R2 调走日：没有任何到场记录；补课日 S1 到
            AttendanceRecord("C1", "2026-03-18", "S1", True),
        ]
        facts = build_facts([self.course], self.sessions, self.enrollments, attendance)
        r2 = facts[("C1", 2)]
        self.assertEqual(r2.expected_sessions, 1)          # 只有补课是应到课次
        self.assertEqual(r2.expected_slots, 1)             # S2 已不在册
        self.assertEqual(r2.present_slots, 1)
        self.assertEqual(r2.absent_slots, 0)               # 调走日无记录不算缺席
        self.assertAlmostEqual(r2.rate, 1.0)

    def test_cancelled_round_is_suspended_and_has_no_rate(self):
        facts = build_facts([self.course], self.sessions, self.enrollments, [])
        r3 = facts[("C1", 3)]
        self.assertTrue(r3.suspended)
        self.assertEqual(r3.expected_sessions, 0)
        self.assertIsNone(r3.rate)

    def test_missing_record_for_eligible_student_counts_as_absence(self):
        attendance = [AttendanceRecord("C1", "2026-03-02", "S1", True)]
        facts = build_facts([self.course], self.sessions, self.enrollments, attendance)
        r1 = facts[("C1", 1)]
        self.assertEqual(r1.present_slots, 1)
        self.assertEqual(r1.absent_slots, 1)
        self.assertIn("S2", r1.affected_students)

    def test_record_from_ineligible_student_is_quarantined_not_silently_dropped(self):
        attendance = [
            AttendanceRecord("C1", "2026-03-18", "S1", True),
            AttendanceRecord("C1", "2026-03-18", "S2", True),  # 当日已不在册
        ]
        facts = build_facts([self.course], self.sessions, self.enrollments, attendance)
        r2 = facts[("C1", 2)]
        self.assertEqual(r2.expected_slots, 1)
        self.assertEqual(len(r2.quarantined), 1)
        self.assertEqual(r2.quarantined[0].record.student_id, "S2")
        self.assertIn("在册", r2.quarantined[0].reason)

    def test_duplicate_records_are_quarantined(self):
        attendance = [
            AttendanceRecord("C1", "2026-03-02", "S1", True),
            AttendanceRecord("C1", "2026-03-02", "S1", False),
            AttendanceRecord("C1", "2026-03-02", "S2", True),
        ]
        facts = build_facts([self.course], self.sessions, self.enrollments, attendance)
        r1 = facts[("C1", 1)]
        self.assertEqual(len(r1.quarantined), 1)
        self.assertEqual(r1.present_slots + r1.absent_slots, 2)


if __name__ == "__main__":
    unittest.main()
