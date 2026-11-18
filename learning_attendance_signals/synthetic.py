"""确定性合成数据：在一个小型社区学校里覆盖全部业务场景。

场景地图（R = 观察轮，每轮一次课）：
- C1 书法入门：R3 起持续恶化的老课程（连续 4 轮，触发持续性确认与升级）；
- C2 声乐提高：R4 单轮波动后自行恢复（只停留在观察中，不产生确定异常）；
- C3 太极班：R4 调课日无到场记录（不应算缺席），当周补课；R5 权威停课
  （监测挂起，恢复后用停课前后自有历史重建基线）；
- C4 智能生活（新开热门课）：R3 才开课，高出勤且份额大，抬高总体率——
  其贡献在分解中单列为新开课效应，不得掩盖 C1 的内部恶化；
- C5 手工班：R5 一次真实下降，运营给了仅当轮有效的人工说明；
  R6 再次真实下降时旧说明不得免责；
- C6/C7/C8/C9：稳定对照课程；
- C10 摄影外拍：R5、R6 连续真实下降，受影响学员与 C1 高度重叠（跨班报名），
  用于验证一个学员不会被多个提醒重复占用联系预算。

出勤人数按目标率确定性取整生成（同规模下最差也只差 1 人次），
保证场景与测试可复现；所有数据均为合成，不含真实个人信息。
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import (
    Annotation,
    AttendanceRecord,
    Course,
    Enrollment,
    ScheduledSession,
    SessionKind,
    Student,
)

ROUNDS = 6
FIRST_DAY = date(2026, 3, 2)     # R1 上课日
ROUND_GAP_DAYS = 14


def round_date(period):
    return FIRST_DAY + timedelta(days=ROUND_GAP_DAYS * (period - 1))


# course_id: (名称, 类别, cohort, 起始轮, 结束轮, startup_rounds)
COURSE_SPEC = [
    ("C1", "书法入门", "书画", "2025秋", 1, 6, 2),
    ("C2", "声乐提高", "表演", "2025秋", 1, 6, 2),
    ("C3", "太极班", "健身", "2025秋", 1, 6, 2),
    ("C4", "智能生活", "科技", "2026春", 3, 6, 2),
    ("C5", "手工班", "生活", "2025秋", 1, 6, 2),
    ("C6", "英语会话", "语言", "2025秋", 1, 6, 2),
    ("C7", "国画赏析", "书画", "2025秋", 1, 6, 2),
    ("C8", "健身操", "健身", "2025秋", 1, 6, 2),
    ("C9", "手机摄影基础", "科技", "2025秋", 1, 6, 2),
    ("C10", "摄影外拍", "科技", "2025秋", 1, 6, 2),
]

CLASS_SIZE = {
    "C1": 20, "C2": 16, "C3": 14, "C4": 26, "C5": 12,
    "C6": 10, "C7": 9, "C8": 15, "C9": 11, "C10": 10,
}

# 各轮"真实"出勤概率；None 表示该轮停课
RATE_CURVE = {
    "C1":  [0.90, 0.90, 0.60, 0.55, 0.50, 0.45],
    "C2":  [0.88, 0.88, 0.87, 0.69, 0.88, 0.87],
    "C3":  [0.86, 0.86, 0.86, None, None, 0.86],
    "C4":  [None, None, 0.96, 0.96, 0.96, 0.96],
    "C5":  [0.85, 0.85, 0.84, 0.85, 0.50, 0.50],
    "C6":  [0.90, 0.90, 0.90, 0.90, 0.90, 0.90],
    "C7":  [0.85, 0.85, 0.86, 0.85, 0.86, 0.85],
    "C8":  [0.88, 0.87, 0.88, 0.87, 0.88, 0.87],
    "C9":  [0.83, 0.84, 0.83, 0.84, 0.83, 0.84],
    "C10": [0.86, 0.86, 0.85, 0.86, 0.55, 0.50],
}

# C10 的 8 名学员同时在 C1（跨班报名）
OVERLAP_WITH_C1 = 8


def _present_count(size, rate):
    """确定性取整：四舍五入到最近整数，保证每轮目标率误差 < 1 人次。"""
    return max(0, min(size, round(size * rate)))


def build_synthetic_dataset():
    courses, students, enrollments, sessions, attendance = [], [], [], [], []

    student_ids = [f"S{i:03d}" for i in range(1, 61)]
    for sid in student_ids:
        students.append(Student(sid, f"学员{sid[1:]}"))

    # 分配学员：C1 使用 S001..S020；C10 前 8 人复用 C1 学员
    roster = {}
    cursor = 21
    for cid, size in CLASS_SIZE.items():
        if cid == "C1":
            roster[cid] = student_ids[:size]
        elif cid == "C10":
            roster[cid] = student_ids[:OVERLAP_WITH_C1] + [
                f"S{cursor + i:03d}" for i in range(size - OVERLAP_WITH_C1)]
            cursor += size - OVERLAP_WITH_C1
        else:
            roster[cid] = [f"S{cursor + i:03d}" for i in range(size)]
            cursor += size
    for extra in sorted(set(sum(roster.values(), [])) - set(student_ids)):
        students.append(Student(extra, f"学员{extra[1:]}"))

    for cid, name, category, cohort, start, end, startup in COURSE_SPEC:
        courses.append(Course(cid, name, category, cohort, startup))
        for sid in roster[cid]:
            enrollments.append(Enrollment(sid, cid,
                                          effective_from=round_date(start).isoformat()))
        for period in range(start, end + 1):
            day = round_date(period)
            rate = RATE_CURVE[cid][period - 1]
            if cid == "C3" and period == 4:
                # 调课：原上课日调走（无应到义务、无到场记录不算缺席），当周补到两天后
                sessions.append(ScheduledSession(
                    cid, day.isoformat(), period, SessionKind.RESCHEDULED_OUT,
                    "教师外出，课程调至本周三"))
                makeup = day + timedelta(days=2)
                sessions.append(ScheduledSession(
                    cid, makeup.isoformat(), period, SessionKind.MAKEUP, "调课补课"))
                _emit_attendance(attendance, cid, makeup.isoformat(),
                                 roster[cid], RATE_CURVE[cid][2])
                continue
            if rate is None:
                sessions.append(ScheduledSession(
                    cid, day.isoformat(), period, SessionKind.CANCELLED,
                    "节假日/场地原因停课"))
                continue
            sessions.append(ScheduledSession(
                cid, day.isoformat(), period, SessionKind.SCHEDULED))
            _emit_attendance(attendance, cid, day.isoformat(), roster[cid], rate)

    annotations = [
        Annotation("A1", "C5", 5, "社区举办手工作品展，多名学员参展缺课", "运营-王老师"),
    ]
    return courses, students, enrollments, sessions, attendance, annotations


def _emit_attendance(sink, course_id, day, member_ids, rate):
    present_n = _present_count(len(member_ids), rate)
    # 让缺课者在轮次间轮换，避免"固定几个人永远缺席"的不真实模式
    offset = sum(ord(ch) for ch in day)
    ordered = member_ids[offset % len(member_ids):] + member_ids[: offset % len(member_ids)]
    present_set = set(ordered[:present_n])
    for sid in member_ids:
        sink.append(AttendanceRecord(course_id, day, sid, sid in present_set,
                                     load_id="synthetic"))
