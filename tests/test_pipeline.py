"""端到端场景：合成数据集上的完整监测-解释-联系-审计故事线。"""
import unittest

from learning_attendance_signals.models import IssueState
from learning_attendance_signals.pipeline import Pipeline
from learning_attendance_signals.report import load_dataset, run
from learning_attendance_signals.storage import InMemoryRepository


def signal_by_course(result, course_id):
    return next(s for s in result["signals"] if s.course_id == course_id)


def issue_by_course(result, course_id):
    return next((i for i in result["issues"] if i.course_id == course_id), None)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = InMemoryRepository()
        load_dataset(cls.repo)
        cls.pipeline, cls.results, cls.facts = run(cls.repo)

    def test_c1_persistent_deterioration_confirmed_after_two_rounds_then_no_improvement(self):
        r3, r4, r5, r6 = (self.results[i - 1] for i in (3, 4, 5, 6))
        self.assertTrue(signal_by_course(r3, "C1").flagged)
        self.assertEqual(issue_by_course(r3, "C1").state, IssueState.WATCHING)
        # R4 连续两轮确认后进入当轮联系批次，快照状态为已联系
        self.assertEqual(issue_by_course(r4, "C1").state, IssueState.CONTACTED)
        r4_attempts = {a.course_id for a in r4["attempts"]}
        self.assertIn("C1", r4_attempts)
        # 联系后证据继续恶化：独立的"联系后无改善"状态被保留，
        # 不与"待联系新问题"混为一种状态，且下一轮仍保持该升级标记
        transitions = [e.details.get("transition")
                       for e in self.pipeline.audit.events(course_id="C1")]
        self.assertIn("no_improvement", transitions)
        self.assertEqual(issue_by_course(r5, "C1").state,
                         IssueState.CONTACTED_NO_IMPROVEMENT)
        self.assertEqual(issue_by_course(r6, "C1").state,
                         IssueState.CONTACTED_NO_IMPROVEMENT)
        # 基线始终锚定恶化前的 0.90，没有被恶化轮稀释
        self.assertAlmostEqual(signal_by_course(r6, "C1").baseline, 0.90)

    def test_c2_single_blip_never_becomes_confirmed_issue(self):
        for result in self.results:
            issue = issue_by_course(result, "C2")
            self.assertTrue(issue is None or issue.state in
                            (IssueState.WATCHING, IssueState.RESOLVED))
        # R5 已自行恢复
        self.assertFalse(signal_by_course(self.results[4], "C2").flagged)

    def test_c3_reschedule_not_absence_and_suspension_resumes_with_stable_baseline(self):
        r4 = self.results[3]
        fact4 = self.facts[("C3", 4)]
        self.assertEqual(fact4.expected_sessions, 1)       # 调走日不应到，补课应到
        self.assertEqual(fact4.quarantined, [])
        self.assertAlmostEqual(fact4.rate, 0.857, places=3)
        r5, r6 = self.results[4], self.results[5]
        self.assertTrue(signal_by_course(r5, "C3").detail["suspended"])
        self.assertTrue(self.facts[("C3", 5)].suspended)
        # 恢复后基线来自停课前后的自有稳定历史，没有与停课轮比较
        self.assertAlmostEqual(signal_by_course(r6, "C3").baseline, 0.857, places=3)

    def test_c4_new_hot_course_lifts_total_but_c1_within_effect_still_visible(self):
        r3 = self.results[2]["decomposition"]
        self.assertGreater(r3["new_course_effect"], 0.15)   # 新开课贡献单列
        self.assertLess(r3["within_effect"], 0)             # C1 内部恶化没有被掩盖
        c4 = signal_by_course(self.results[2], "C4")
        self.assertTrue(c4.startup)
        self.assertFalse(c4.flagged)                        # 不套用别人的稳定程度

    def test_c5_manual_annotation_suppresses_only_that_round(self):
        r5, r6 = self.results[4], self.results[5]
        s5 = signal_by_course(r5, "C5")
        self.assertTrue(s5.suppressed)
        self.assertIn("参展", s5.suppression_reason)
        s6 = signal_by_course(r6, "C5")
        self.assertFalse(s6.suppressed)                     # 旧说明不能免责
        self.assertTrue(s6.flagged)
        # R5 抑制轮不进基线，R6 基线仍是稳定历史（10/12 与 10/12、11/12 的加权）
        self.assertAlmostEqual(s6.baseline, 0.833, places=2)

    def test_c10_overlap_does_not_double_spend_contact_budget(self):
        r6 = self.results[5]
        attempts = {a.course_id: a for a in r6["attempts"]}
        self.assertIn("C1", attempts)
        self.assertIn("C10", attempts)
        overlap = set(attempts["C1"].students) & set(attempts["C10"].students)
        self.assertEqual(overlap, set())                    # 同轮跨班学员只被一份联系覆盖
        # C10 的 8 名重叠学员已在 C1 的历史联系中处理，C10 只能联系到 2 名非重叠学员
        self.assertEqual(len(attempts["C10"].students), 2)
        # C1 本轮跳过的 8 人正是与 C10 重叠的那批（历史已处理，不重复占用预算）
        self.assertEqual(attempts["C1"].score_breakdown["duplicate_students_skipped"], 8)
        # 全流程联系名单中学员不重复
        all_contacted = [s for a in self.repo.list_contacts() for s in a.students]
        self.assertEqual(len(all_contacted), len(set(all_contacted)))

    def test_contact_budget_is_two_classes_per_round(self):
        for result in self.results:
            self.assertLessEqual(len(result["attempts"]), 2)

    def test_audit_explains_both_presence_and_absence_of_alerts(self):
        events = self.pipeline.audit.events(course_id="C1")
        types = [e.event_type for e in events]
        self.assertIn("monitor_flagged", types)
        self.assertIn("monitor_clear", types)
        # 没报警的轮次也有可核对理由
        clear = next(e for e in events if e.event_type == "monitor_clear")
        self.assertTrue(clear.reason.startswith("未触发提醒"))
        # 联系决策带分数拆解
        planned = self.pipeline.audit.events(event_type="contact_planned")
        self.assertTrue(planned)
        self.assertIn("score_breakdown", planned[0].details)

    def test_audit_traces_why_each_batch_was_ranked_and_budget_skips(self):
        # R6 预算为 2：入选批次与预算外跳过都有留痕
        r6_events = self.pipeline.audit.events(period=6)
        planned = [e for e in r6_events if e.event_type == "contact_planned"]
        self.assertEqual(len(planned), 2)
        for event in planned:
            breakdown = event.details["score_breakdown"]
            # 分数拆解完整，能解释排序依据
            self.assertTrue(all(key in breakdown for key in
                                ("evidence_weight", "coverage_weight",
                                 "persistence_bonus", "no_improvement_bonus",
                                 "duplicate_students_skipped")))
        # 预算外课程的跳过原因同样留痕（无跳过时该列表为空，可解释为预算未饱和）
        skipped = {e.course_id: e.reason
                   for e in self.pipeline.audit.events(event_type="contact_skipped")}
        r6_actionable = [i for i in self.results[5]["issues"]
                         if i.state.value in ("待联系", "持续未联系", "联系后无改善", "已联系")]
        if len(r6_actionable) > 2:
            self.assertTrue(skipped)
        else:
            self.assertEqual(
                [e for e in r6_events if e.event_type == "contact_skipped"], [])
        # 状态迁移链可重放：C1 经历观察→确认→联系→无改善
        c1_transitions = [e.details["transition"]
                          for e in self.pipeline.audit.events(course_id="C1")
                          if e.event_type == "issue_state"]
        self.assertEqual(c1_transitions[0], "watching")
        self.assertIn("confirmed", c1_transitions)
        self.assertIn("no_improvement", c1_transitions)

    def test_raw_attendance_remains_immutable_and_derived_values_are_separate(self):
        raw = self.repo.list_attendance()
        self.assertTrue(all(record.load_id == "synthetic" for record in raw))
        # 派生事实存在且与原始账分离
        facts = self.repo.list_facts()
        self.assertTrue(facts)
        # 原始记录重复写入被拒绝
        from learning_attendance_signals.models import AttendanceRecord
        with self.assertRaises(ValueError):
            self.repo.add_attendance(
                AttendanceRecord(raw[0].course_id, raw[0].session_date,
                                 raw[0].student_id, raw[0].present, raw[0].load_id))


if __name__ == "__main__":
    unittest.main()
