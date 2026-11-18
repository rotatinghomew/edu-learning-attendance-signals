"""SQLite 仓储测试：原始账只增不改、派生物分表、两种存储结论一致。"""
import tempfile
import unittest
from pathlib import Path

from learning_attendance_signals.models import (
    AuditEvent,
    ContactAttempt,
    Course,
    CourseFact,
    Enrollment,
    EvidenceGrade,
    Issue,
    IssueState,
    ScheduledSession,
    SessionKind,
    Signal,
    Student,
)
from learning_attendance_signals.pipeline import Pipeline
from learning_attendance_signals.report import load_dataset, run
from learning_attendance_signals.storage import InMemoryRepository, SqliteRepository


class SqliteRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = SqliteRepository(Path(self.directory.name) / "test.db")

    def tearDown(self):
        self.repo.close()
        self.directory.cleanup()

    def test_master_data_round_trip(self):
        course = Course("C1", "书法", "书画", "2025秋", 2)
        student = Student("S1", "学员01")
        self.repo.upsert_course(course)
        self.repo.upsert_student(student)
        self.repo.add_enrollment(Enrollment("S1", "C1", "2026-01-01"))
        self.repo.add_session(ScheduledSession(
            "C1", "2026-03-02", 1, SessionKind.SCHEDULED))
        self.assertEqual(self.repo.list_courses()[0], course)
        self.assertEqual(self.repo.list_students()[0], student)
        self.assertEqual(self.repo.list_enrollments()[0].student_id, "S1")
        self.assertEqual(self.repo.list_sessions()[0].kind, SessionKind.SCHEDULED)

    def test_derived_tables_keep_basis_and_do_not_overwrite_raw(self):
        from learning_attendance_signals.models import AttendanceRecord
        record = AttendanceRecord("C1", "2026-03-02", "S1", True, "load-1")
        self.repo.add_attendance(record)
        with self.assertRaises(ValueError):
            self.repo.add_attendance(record)  # 重复写入被拒绝

        fact = CourseFact("C1", 1, expected_sessions=1, expected_slots=1,
                          present_slots=1, affected_students=(),
                          expected_students=("S1",))
        self.repo.save_fact(fact)
        loaded = self.repo.get_fact("C1", 1)
        self.assertEqual(loaded.present_slots, 1)
        # 原始表仍是一条、未被派生流程修改
        self.assertEqual(len(self.repo.list_attendance()), 1)

        signal = Signal(course_id="C1", period=1, rate=1.0, baseline=None,
                        expected_slots=1, affected_students=(),
                        grade=EvidenceGrade.NONE)
        self.repo.save_signal(signal)
        self.assertEqual(self.repo.list_signals(period=1)[0].course_id, "C1")

    def test_issue_and_contact_persistence(self):
        issue = Issue("ISSUE-C1", "C1", 3, IssueState.OPEN_NEW, 2, 4)
        self.repo.save_issue(issue)
        self.assertEqual(self.repo.list_issues()[0].state, IssueState.OPEN_NEW)
        attempt = ContactAttempt("ISSUE-C1", "C1", 4, ("S1", "S2"),
                                 EvidenceGrade.STRONG, 4.0, {"evidence_weight": 3.0})
        self.repo.add_contact(attempt)
        self.assertEqual(self.repo.list_contacts(period=4)[0].students, ("S1", "S2"))

    def test_audit_query_filters(self):
        self.repo.append_audit(AuditEvent(None, 1, "C1", "monitor_flagged", "r", {"k": 1}))
        self.repo.append_audit(AuditEvent(None, 2, "C2", "contact_planned", "r", {}))
        self.assertEqual(len(self.repo.list_audit()), 2)
        self.assertEqual(len(self.repo.list_audit(course_id="C1")), 1)
        self.assertEqual(len(self.repo.list_audit(period=2)), 1)
        self.assertEqual(len(self.repo.list_audit(event_type="contact_planned")), 1)


class StorageParityTests(unittest.TestCase):
    def test_memory_and_sqlite_produce_identical_conclusions(self):
        memory = InMemoryRepository()
        load_dataset(memory)
        _, memory_results, _ = run(memory)

        with tempfile.TemporaryDirectory() as directory:
            sql_repo = SqliteRepository(Path(directory) / "parity.db")
            load_dataset(sql_repo)
            sql_pipeline, sql_results, _ = run(sql_repo)
            sql_raw = len(sql_repo.list_attendance())
            sql_repo.close()

        self.assertEqual(sql_raw, len(memory.list_attendance()))
        for mem, sql in zip(memory_results, sql_results):
            mem_flags = sorted((s.course_id, s.flagged, s.grade.value, s.suppressed)
                               for s in mem["signals"])
            sql_flags = sorted((s.course_id, s.flagged, s.grade.value, s.suppressed)
                               for s in sql["signals"])
            self.assertEqual(mem_flags, sql_flags)
            self.assertEqual(
                sorted((i.course_id, i.state.value) for i in mem["issues"]),
                sorted((i.course_id, i.state.value) for i in sql["issues"]))
            self.assertEqual(
                [(a.course_id, a.students, round(a.score, 4)) for a in mem["attempts"]],
                [(a.course_id, a.students, round(a.score, 4)) for a in sql["attempts"]])

    def test_pipeline_is_replayable(self):
        repo = InMemoryRepository()
        load_dataset(repo)
        first = Pipeline(repo).run_all()
        # 同输入重放：新建仓储再跑一遍，信号判定完全一致
        replay_repo = InMemoryRepository()
        load_dataset(replay_repo)
        second = Pipeline(replay_repo).run_all()
        self.assertEqual(
            [(s.course_id, s.period, s.flagged) for r in first for s in r["signals"]],
            [(s.course_id, s.period, s.flagged) for r in second for s in r["signals"]])


if __name__ == "__main__":
    unittest.main()
