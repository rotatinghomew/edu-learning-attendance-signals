"""总体出勤率变化分解：区分"构成变化"与"同类课程内部恶化"。

对相邻两轮 t-1 → t，按课程份额 w = expected_slots / N 加权：
  Δ总体 =  Σ w_prev·(r_cur - r_prev)                 内部效应（同课程出勤变化）
        + Σ (w_cur - w_prev)·r_prev                  构成效应（份额变化）
        + Σ (w_cur - w_prev)·(r_cur - r_prev)        交互项
新开课（上轮无应到记录）与停课课程单独列示，不混入内部效应；
"新开热门课抬高总量"会表现为正向的新开课贡献，而老课程的内部恶化仍单独可见。
"""
from __future__ import annotations


def _rates(facts, period):
    rows = {}
    total_slots = 0
    total_present = 0
    for (course_id, p), fact in facts.items():
        if p != period or fact.suspended or fact.expected_slots == 0:
            continue
        rows[course_id] = fact
        total_slots += fact.expected_slots
        total_present += fact.present_slots
    overall = total_present / total_slots if total_slots else None
    return rows, total_slots, overall


def decompose(facts, prev_period, cur_period):
    prev, prev_n, prev_overall = _rates(facts, prev_period)
    cur, cur_n, cur_overall = _rates(facts, cur_period)
    if prev_overall is None or cur_overall is None:
        return None

    within = composition = interaction = 0.0
    course_contributions = []
    common = set(prev) & set(cur)
    for course_id in sorted(common):
        a, b = prev[course_id], cur[course_id]
        w_prev, w_cur = a.expected_slots / prev_n, b.expected_slots / cur_n
        rate_prev, rate_cur = a.rate, b.rate
        within_term = w_prev * (rate_cur - rate_prev)
        comp_term = (w_cur - w_prev) * rate_prev
        inter_term = (w_cur - w_prev) * (rate_cur - rate_prev)
        within += within_term
        composition += comp_term
        interaction += inter_term
        course_contributions.append({
            "course_id": course_id,
            "rate_prev": rate_prev,
            "rate_cur": rate_cur,
            "share_prev": w_prev,
            "share_cur": w_cur,
            "within": within_term,
            "composition": comp_term,
            "interaction": inter_term,
        })

    new_courses, suspended_courses = [], []
    for course_id, fact in cur.items():
        if course_id not in prev:
            new_courses.append({
                "course_id": course_id,
                "rate": fact.rate,
                "share": fact.expected_slots / cur_n,
                "contribution": (fact.expected_slots / cur_n) * fact.rate,
            })
    for course_id, fact in prev.items():
        if course_id not in cur:
            suspended_courses.append({
                "course_id": course_id,
                "prev_rate": fact.rate,
                "prev_share": fact.expected_slots / prev_n,
                "contribution": -(fact.expected_slots / prev_n) * fact.rate,
            })

    explained = within + composition + interaction
    return {
        "prev_period": prev_period,
        "cur_period": cur_period,
        "overall_prev": prev_overall,
        "overall_cur": cur_overall,
        "overall_change": cur_overall - prev_overall,
        "within_effect": within,
        "composition_effect": composition,
        "interaction_effect": interaction,
        "new_course_effect": sum(item["contribution"] for item in new_courses),
        "removed_course_effect": sum(item["contribution"] for item in suspended_courses),
        "reconstructed_change": explained,
        "courses": course_contributions,
        "new_courses": new_courses,
        "removed_courses": suspended_courses,
        "unexplained_residual": (cur_overall - prev_overall) - explained
        - sum(item["contribution"] for item in new_courses)
        - sum(item["contribution"] for item in suspended_courses),
    }


def affected_headcount(facts, signals, cur_period):
    """受影响人数：内部恶化（本轮被标记）课程涉及的去重在册学员并集。"""
    students = set()
    for signal in signals:
        if signal.period != cur_period or not signal.flagged or signal.suppressed:
            continue
        students.update(signal.affected_students)
    return len(students)
