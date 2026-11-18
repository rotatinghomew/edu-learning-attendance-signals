"""持久化仓储：原始出勤账只增不改，派生事实/信号/审计分表存放并标注口径。

提供两种实现、同一接口：
- InMemoryRepository：测试与快速演练；
- SqliteRepository：本地 SQLite，原始 CSV 导入后不受派生流程影响。
"""
from __future__ import annotations

import sqlite3

from .models import (
    Annotation,
    AttendanceRecord,
    ContactAttempt,
    Course,
    CourseFact,
    Enrollment,
    Issue,
    IssueState,
    ScheduledSession,
    SessionKind,
    Signal,
    Student,
)


class InMemoryRepository:
    def __init__(self):
        self.courses = {}
        self.students = {}
        self.enrollments = []
        self.sessions = []
        self.attendance = []
        self.annotations = {}
        self.facts = {}
        self.signals = {}
        self.issues = {}
        self.contacts = []
        self.audit_events = []

    # ---- 主数据 ----
    def upsert_course(self, course: Course):
        self.courses[course.course_id] = course

    def list_courses(self):
        return [self.courses[k] for k in sorted(self.courses)]

    def upsert_student(self, student: Student):
        self.students[student.student_id] = student

    def list_students(self):
        return [self.students[k] for k in sorted(self.students)]

    def add_enrollment(self, enrollment: Enrollment):
        self.enrollments.append(enrollment)

    def list_enrollments(self):
        return list(self.enrollments)

    def add_session(self, session: ScheduledSession):
        self.sessions.append(session)

    def list_sessions(self):
        return list(self.sessions)

    def add_attendance(self, record: AttendanceRecord):
        key = (record.course_id, record.session_date, record.student_id, record.load_id)
        if any((r.course_id, r.session_date, r.student_id, r.load_id) == key
               for r in self.attendance):
            raise ValueError(f"原始出勤记录重复写入被拒绝: {key}")
        self.attendance.append(record)

    def list_attendance(self):
        return list(self.attendance)

    def add_annotation(self, annotation: Annotation):
        key = (annotation.course_id, annotation.period)
        if key in self.annotations:
            raise ValueError(f"同一轮次重复人工说明: {key}")
        self.annotations[key] = annotation

    def list_annotations(self):
        return list(self.annotations.values())

    # ---- 派生数据（与原始账分库表存放）----
    def save_fact(self, fact: CourseFact):
        self.facts[(fact.course_id, fact.period)] = fact

    def get_fact(self, course_id, period):
        return self.facts.get((course_id, period))

    def list_facts(self):
        return [self.facts[k] for k in sorted(self.facts)]

    def save_signal(self, signal: Signal):
        self.signals[(signal.course_id, signal.period)] = signal

    def list_signals(self, period=None):
        values = self.signals.values()
        if period is not None:
            values = [s for s in values if s.period == period]
        return list(values)

    def save_issue(self, issue: Issue):
        self.issues[issue.issue_id] = issue

    def list_issues(self):
        return list(self.issues.values())

    def add_contact(self, attempt: ContactAttempt):
        self.contacts.append(attempt)

    def list_contacts(self, period=None):
        if period is None:
            return list(self.contacts)
        return [c for c in self.contacts if c.period == period]

    # ---- 审计 ----
    def append_audit(self, event):
        self.audit_events.append(event)

    def list_audit(self, course_id=None, period=None, event_type=None):
        result = self.audit_events
        if course_id is not None:
            result = [e for e in result if e.course_id == course_id]
        if period is not None:
            result = [e for e in result if e.period == period]
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        return list(result)


class SqliteRepository:
    def __init__(self, path):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def close(self):
        self._conn.close()

    def _create_schema(self):
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                course_id TEXT PRIMARY KEY, name TEXT, category TEXT,
                cohort TEXT, startup_rounds INTEGER);
            CREATE TABLE IF NOT EXISTS students (student_id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS enrollments (
                student_id TEXT, course_id TEXT,
                effective_from TEXT, effective_to TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
                course_id TEXT, session_date TEXT, period INTEGER,
                kind TEXT, note TEXT,
                PRIMARY KEY (course_id, session_date));
            -- 原始出勤账：只增不改，load_id 保留批次来源便于核对
            CREATE TABLE IF NOT EXISTS attendance_raw (
                course_id TEXT, session_date TEXT, student_id TEXT,
                present INTEGER, load_id TEXT,
                PRIMARY KEY (course_id, session_date, student_id, load_id));
            CREATE TABLE IF NOT EXISTS annotations (
                annotation_id TEXT PRIMARY KEY, course_id TEXT, period INTEGER,
                reason TEXT, author TEXT);
            -- 以下均为派生产物，basis 标注口径，绝不伪装成真实人次
            CREATE TABLE IF NOT EXISTS facts_derived (
                course_id TEXT, period INTEGER, basis TEXT DEFAULT 'actual_due',
                expected_sessions INTEGER, expected_slots INTEGER,
                present_slots INTEGER, absent_slots INTEGER,
                expected_students TEXT, affected_students TEXT,
                PRIMARY KEY (course_id, period, basis));
            CREATE TABLE IF NOT EXISTS signals_derived (
                course_id TEXT, period INTEGER,
                rate REAL, baseline REAL, expected_slots INTEGER,
                affected_students TEXT, p_value REAL, adjusted_p REAL,
                effect_drop REAL, grade TEXT, flagged INTEGER,
                startup INTEGER, suppressed INTEGER, suppression_reason TEXT,
                reasons TEXT, detail TEXT,
                PRIMARY KEY (course_id, period));
            CREATE TABLE IF NOT EXISTS issues (
                issue_id TEXT PRIMARY KEY, course_id TEXT, opened_period INTEGER,
                state TEXT, streak INTEGER, last_period INTEGER);
            CREATE TABLE IF NOT EXISTS contacts (
                issue_id TEXT, course_id TEXT, period INTEGER,
                students TEXT, grade TEXT, score REAL, breakdown TEXT);
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, period INTEGER,
                course_id TEXT, event_type TEXT, reason TEXT, details TEXT);
            """
        )
        c.commit()

    # ---- 主数据 ----
    def upsert_course(self, course: Course):
        self._conn.execute(
            "INSERT OR REPLACE INTO courses VALUES (?, ?, ?, ?, ?)",
            (course.course_id, course.name, course.category, course.cohort,
             course.startup_rounds))
        self._conn.commit()

    def list_courses(self):
        return [Course(r["course_id"], r["name"], r["category"], r["cohort"],
                       r["startup_rounds"]) for r in
                self._conn.execute("SELECT * FROM courses ORDER BY course_id")]

    def upsert_student(self, student: Student):
        self._conn.execute(
            "INSERT OR REPLACE INTO students VALUES (?, ?)",
            (student.student_id, student.name))
        self._conn.commit()

    def list_students(self):
        return [Student(r["student_id"], r["name"])
                for r in self._conn.execute("SELECT * FROM students ORDER BY student_id")]

    def add_enrollment(self, enrollment: Enrollment):
        self._conn.execute(
            "INSERT INTO enrollments VALUES (?, ?, ?, ?)",
            (enrollment.student_id, enrollment.course_id,
             enrollment.effective_from, enrollment.effective_to))
        self._conn.commit()

    def list_enrollments(self):
        return [Enrollment(r["student_id"], r["course_id"],
                           r["effective_from"], r["effective_to"])
                for r in self._conn.execute("SELECT * FROM enrollments ORDER BY student_id, course_id")]

    def add_session(self, session: ScheduledSession):
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)",
            (session.course_id, session.session_date, session.period,
             session.kind.value, session.note))
        self._conn.commit()

    def list_sessions(self):
        return [ScheduledSession(r["course_id"], r["session_date"], r["period"],
                                 SessionKind(r["kind"]), r["note"])
                for r in self._conn.execute("SELECT * FROM sessions ORDER BY course_id, session_date")]

    def add_attendance(self, record: AttendanceRecord):
        try:
            self._conn.execute(
                "INSERT INTO attendance_raw VALUES (?, ?, ?, ?, ?)",
                (record.course_id, record.session_date, record.student_id,
                 1 if record.present else 0, record.load_id))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"原始出勤记录重复写入被拒绝: {record}") from exc

    def list_attendance(self):
        return [AttendanceRecord(r["course_id"], r["session_date"], r["student_id"],
                                 bool(r["present"]), r["load_id"])
                for r in self._conn.execute("SELECT * FROM attendance_raw ORDER BY course_id, session_date, student_id")]

    def add_annotation(self, annotation: Annotation):
        try:
            self._conn.execute(
                "INSERT INTO annotations VALUES (?, ?, ?, ?, ?)",
                (annotation.annotation_id, annotation.course_id, annotation.period,
                 annotation.reason, annotation.author))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"同一轮次重复人工说明: {annotation.course_id} R{annotation.period}") from exc

    def list_annotations(self):
        return [Annotation(r["annotation_id"], r["course_id"], r["period"],
                           r["reason"], r["author"])
                for r in self._conn.execute("SELECT * FROM annotations ORDER BY course_id, period")]

    # ---- 派生数据 ----
    def save_fact(self, fact: CourseFact):
        import json
        self._conn.execute(
            "INSERT OR REPLACE INTO facts_derived VALUES (?, ?, 'actual_due', ?, ?, ?, ?, ?, ?)",
            (fact.course_id, fact.period, fact.expected_sessions, fact.expected_slots,
             fact.present_slots, fact.absent_slots,
             json.dumps(list(fact.expected_students), ensure_ascii=False),
             json.dumps(list(fact.affected_students), ensure_ascii=False)))
        self._conn.commit()

    def get_fact(self, course_id, period):
        r = self._conn.execute(
            "SELECT * FROM facts_derived WHERE course_id=? AND period=?",
            (course_id, period)).fetchone()
        if r is None:
            return None
        import json
        return CourseFact(r["course_id"], r["period"], r["expected_sessions"],
                          r["expected_slots"], r["present_slots"], r["absent_slots"],
                          tuple(json.loads(r["expected_students"])),
                          tuple(json.loads(r["affected_students"])))

    def list_facts(self):
        return [self.get_fact(r["course_id"], r["period"])
                for r in self._conn.execute(
                    "SELECT course_id, period FROM facts_derived ORDER BY course_id, period")]

    def save_signal(self, signal: Signal):
        import json
        self._conn.execute(
            "INSERT OR REPLACE INTO signals_derived VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signal.course_id, signal.period, signal.rate, signal.baseline,
             signal.expected_slots, json.dumps(list(signal.affected_students), ensure_ascii=False),
             signal.p_value, signal.adjusted_p, signal.effect_drop,
             signal.grade.value, int(signal.flagged), int(signal.startup),
             int(signal.suppressed), signal.suppression_reason,
             json.dumps(signal.reasons, ensure_ascii=False),
             json.dumps(signal.detail, ensure_ascii=False)))
        self._conn.commit()

    def list_signals(self, period=None):
        import json
        rows = self._conn.execute(
            "SELECT * FROM signals_derived WHERE (? IS NULL OR period=?) ORDER BY course_id, period",
            (period, period)).fetchall()
        from .models import EvidenceGrade
        signals = []
        for r in rows:
            signals.append(Signal(
                course_id=r["course_id"], period=r["period"], rate=r["rate"],
                baseline=r["baseline"], expected_slots=r["expected_slots"],
                affected_students=tuple(json.loads(r["affected_students"])),
                p_value=r["p_value"], adjusted_p=r["adjusted_p"],
                effect_drop=r["effect_drop"], grade=EvidenceGrade(r["grade"]),
                flagged=bool(r["flagged"]), startup=bool(r["startup"]),
                suppressed=bool(r["suppressed"]),
                suppression_reason=r["suppression_reason"],
                reasons=json.loads(r["reasons"]), detail=json.loads(r["detail"])))
        return signals

    def save_issue(self, issue: Issue):
        self._conn.execute(
            "INSERT OR REPLACE INTO issues VALUES (?, ?, ?, ?, ?, ?)",
            (issue.issue_id, issue.course_id, issue.opened_period,
             issue.state.value, issue.streak, issue.last_period))
        self._conn.commit()

    def list_issues(self):
        return [Issue(r["issue_id"], r["course_id"], r["opened_period"],
                      IssueState(r["state"]), r["streak"], r["last_period"])
                for r in self._conn.execute("SELECT * FROM issues ORDER BY issue_id")]

    def add_contact(self, attempt: ContactAttempt):
        import json
        self._conn.execute(
            "INSERT INTO contacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt.issue_id, attempt.course_id, attempt.period,
             json.dumps(list(attempt.students), ensure_ascii=False),
             attempt.evidence_grade.value, attempt.score,
             json.dumps(attempt.score_breakdown, ensure_ascii=False)))
        self._conn.commit()

    def list_contacts(self, period=None):
        import json
        from .models import EvidenceGrade
        rows = self._conn.execute(
            "SELECT * FROM contacts WHERE (? IS NULL OR period=?) ORDER BY score DESC",
            (period, period)).fetchall()
        return [ContactAttempt(
            r["issue_id"], r["course_id"], r["period"],
            tuple(json.loads(r["students"])), EvidenceGrade(r["grade"]),
            r["score"], json.loads(r["breakdown"])) for r in rows]

    # ---- 审计 ----
    def append_audit(self, event):
        import json
        cur = self._conn.execute(
            "INSERT INTO audit_log (period, course_id, event_type, reason, details) VALUES (?, ?, ?, ?, ?)",
            (event.period, event.course_id, event.event_type, event.reason,
             json.dumps(event.details, ensure_ascii=False)))
        self._conn.commit()
        event.seq = cur.lastrowid
        return event

    def list_audit(self, course_id=None, period=None, event_type=None):
        import json
        from .models import AuditEvent
        rows = self._conn.execute(
            """SELECT * FROM audit_log
               WHERE (? IS NULL OR course_id=?)
                 AND (? IS NULL OR period=?)
                 AND (? IS NULL OR event_type=?)
               ORDER BY seq""",
            (course_id, course_id, period, period, event_type, event_type)).fetchall()
        return [AuditEvent(r["seq"], r["period"], r["course_id"], r["event_type"],
                           r["reason"], json.loads(r["details"])) for r in rows]
