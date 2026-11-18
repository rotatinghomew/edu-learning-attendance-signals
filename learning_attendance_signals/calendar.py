"""出勤事实口径：把原始账折算为"实际应到"口径。

关键规则：
- RESCHEDULED_OUT / CANCELLED 课次不产生应到义务，当日无到场记录不是缺席。
- MAKEUP 课次是真实应到课次，与正常课次同口径。
- 只有在课次当日处于在册有效期内的学员计入分母。
- 与应到安排或在册资格对不上的原始记录进入隔离区，绝不静默丢弃或修正。
"""
from __future__ import annotations

from collections import defaultdict

from .models import (
    Course,
    CourseFact,
    Enrollment,
    QuarantinedRecord,
    ScheduledSession,
    SessionKind,
)

DUE_KINDS = {SessionKind.SCHEDULED, SessionKind.MAKEUP}


def _enrollment_index(enrollments):
    index = defaultdict(list)
    for enrollment in enrollments:
        index[enrollment.course_id].append(enrollment)
    return index


def build_facts(courses, sessions, enrollments, attendance):
    """返回 {(course_id, period): CourseFact}，覆盖每门课每个出现过的轮次。"""
    enroll_by_course = _enrollment_index(enrollments)

    due_sessions = defaultdict(list)          # course -> 应到课次
    all_periods = defaultdict(set)            # course -> 出现过的轮次（含停课轮）
    for session in sessions:
        all_periods[session.course_id].add(session.period)
        if session.kind in DUE_KINDS:
            due_sessions[session.course_id].append(session)

    attendance_index = defaultdict(list)
    for record in attendance:
        attendance_index[(record.course_id, record.session_date)].append(record)

    facts = {}
    for course in courses:
        active = enroll_by_course.get(course.course_id, [])
        for period in sorted(all_periods.get(course.course_id, ())):
            facts[(course.course_id, period)] = _build_course_fact(
                course, period, due_sessions, active, attendance_index
            )
    return facts


def _eligible(enrollments, day):
    return [e for e in enrollments if e.covers(day)]


def _build_course_fact(course: Course, period, due_sessions, enrollments, attendance_index):
    period_sessions = [s for s in due_sessions.get(course.course_id, []) if s.period == period]
    fact = CourseFact(
        course_id=course.course_id,
        period=period,
        expected_sessions=len(period_sessions),
    )
    if not period_sessions:
        return fact  # 整轮无应到课次（停课/调课）：suspended，不产生率

    expected_students = set()
    affected = set()
    for session in sorted(period_sessions, key=lambda s: s.session_date):
        eligible = _eligible(enrollments, session.session_date)
        expected_ids = {e.student_id for e in eligible}
        expected_students |= expected_ids
        fact.expected_slots += len(eligible)

        raw_records = attendance_index.get((course.course_id, session.session_date), [])
        seen = set()
        for record in raw_records:
            if record.student_id in seen:
                fact.quarantined.append(
                    QuarantinedRecord(record, "同一课次重复出勤记录")
                )
                continue
            seen.add(record.student_id)
            if record.student_id not in expected_ids:
                fact.quarantined.append(
                    QuarantinedRecord(record, "课次当日无在册资格")
                )
                continue
            if record.present:
                fact.present_slots += 1
            else:
                fact.absent_slots += 1
                affected.add(record.student_id)

        # 应有账而缺失的在册学员：按缺席处理但保留为可核对的缺口，而不是伪造记录
        accounted = {r.student_id for r in raw_records if r.student_id in expected_ids}
        missing = expected_ids - accounted
        fact.absent_slots += len(missing)
        affected |= missing

    fact.expected_students = tuple(sorted(expected_students))
    fact.affected_students = tuple(sorted(affected))
    return fact
