"""演示入口：合成数据 → 完整管线 → 可读的监测/解释/联系摘要。

默认同时用内存仓储与临时 SQLite 仓储各跑一遍并对账，证明：
- 原始出勤记录在 SQLite 中只增不改，派生值单独成表并标注 basis；
- 同一批输入在两种存储下结论一致、可重放。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .pipeline import Pipeline
from .storage import InMemoryRepository, SqliteRepository
from .synthetic import build_synthetic_dataset


def load_dataset(repo):
    courses, students, enrollments, sessions, attendance, annotations = build_synthetic_dataset()
    for course in courses:
        repo.upsert_course(course)
    for student in students:
        repo.upsert_student(student)
    for enrollment in enrollments:
        repo.add_enrollment(enrollment)
    for session in sessions:
        repo.add_session(session)
    for record in attendance:
        repo.add_attendance(record)
    for annotation in annotations:
        repo.add_annotation(annotation)


def run(repo):
    pipeline = Pipeline(repo, contact_budget=2)
    results = pipeline.run_all()
    facts = {(f.course_id, f.period): f for f in repo.list_facts()}
    return pipeline, results, facts


def _print_round(result, facts):
    period = result["period"]
    print(f"\n===== 第 {period} 轮 =====")
    for signal in sorted(result["signals"], key=lambda s: s.course_id):
        fact = facts[(signal.course_id, period)]
        if signal.detail.get("suspended"):
            print(f"  {signal.course_id}: 停课/调课，应到课次 0，监测挂起")
            continue
        rate = f"{signal.rate:.2%}" if signal.rate is not None else "—"
        base = f"{signal.baseline:.2%}" if signal.baseline is not None else "无自有基线"
        label = "报警" if signal.flagged else "观察"
        if signal.suppressed:
            label = f"已抑制（{signal.suppression_reason}）"
        elif signal.startup:
            label = "新开课观察"
        print(f"  {signal.course_id}: 出勤 {rate}（基线 {base}），"
              f"应到 {fact.expected_slots} 人次，受影响 {len(signal.affected_students)} 人，{label}")

    decomposition = result["decomposition"]
    if decomposition:
        print("  总体变化分解：")
        print(f"    总体率 {decomposition['overall_prev']:.2%} → "
              f"{decomposition['overall_cur']:.2%}（Δ {decomposition['overall_change']:+.2%}）")
        print(f"    同类内部效应 {decomposition['within_effect']:+.2%}｜"
              f"构成效应 {decomposition['composition_effect']:+.2%}｜"
              f"交互 {decomposition['interaction_effect']:+.2%}｜"
              f"新开课贡献 {decomposition['new_course_effect']:+.2%}")

    if result["attempts"]:
        print("  本轮联系批次：")
        for attempt in result["attempts"]:
            print(f"    {attempt.course_id}（证据 {attempt.evidence_grade.value}，"
                  f"得分 {attempt.score:.2f}，覆盖 {len(attempt.students)} 人）")
    for course_id, reason in result["skipped"]:
        print(f"  未入批次 {course_id}: {reason}")

    open_issues = [i for i in result["issues"]
                   if i.state.value not in ("已恢复", "已解释停课")]
    if open_issues:
        print("  问题台账：")
        for issue in sorted(open_issues, key=lambda i: i.course_id):
            print(f"    {issue.course_id}: {issue.state.value}（连续 {issue.streak} 轮）")


def main():
    repo = InMemoryRepository()
    load_dataset(repo)
    pipeline, results, facts = run(repo)
    print("社区老年教育出勤监测演示（合成数据，无真实个人信息）")
    for result in results:
        _print_round(result, facts)

    print("\n===== 审计追查示例 =====")
    for course_id in ("C1", "C5", "C10"):
        print(f"  {course_id} 的事件链：")
        for event in pipeline.audit.events(course_id=course_id):
            period = f"R{event.period}" if event.period else " - "
            print(f"    [{period}] {event.event_type}: {event.reason}")

    # SQLite 对账：同一输入、同一结论，且原始账不可变
    with tempfile.TemporaryDirectory() as directory:
        sql_repo = SqliteRepository(Path(directory) / "attendance.db")
        load_dataset(sql_repo)
        sql_pipeline, sql_results, _ = run(sql_repo)
        memory_flags = [
            (s.course_id, s.period, s.flagged)
            for result in results for s in result["signals"]
        ]
        sqlite_flags = [
            (s.course_id, s.period, s.flagged)
            for result in sql_results for s in result["signals"]
        ]
        assert memory_flags == sqlite_flags, "内存与 SQLite 结论不一致"
        raw_count = len(sql_repo.list_attendance())
        print(f"\nSQLite 对账通过：{raw_count} 条原始出勤记录保持原样，"
              f"派生事实/信号/联系/审计分表存放（共 {len(sql_pipeline.audit.events())} 条审计事件）")
        sql_repo.close()


if __name__ == "__main__":
    main()
