"""总体率变化分解测试。"""
import unittest

from learning_attendance_signals.decomposition import affected_headcount, decompose
from learning_attendance_signals.models import CourseFact, Signal


def fact(course_id, period, slots, present):
    return CourseFact(course_id, period, expected_sessions=1,
                      expected_slots=slots, present_slots=present,
                      absent_slots=slots - present)


class DecompositionTests(unittest.TestCase):
    def test_internal_deterioration_is_visible_even_when_hot_new_course_lifts_total(self):
        facts = {
            # 老课程 A：份额小、内部恶化
            ("A", 1): fact("A", 1, 10, 9),
            ("A", 2): fact("A", 2, 10, 5),
            # 新开热门课 B：第二轮才出现，高出勤、大份额
            ("B", 2): fact("B", 2, 90, 88),
        }
        result = decompose(facts, 1, 2)
        # 总体率被抬高（0.9 → 0.93），但内部效应必须独立显示为负
        self.assertGreater(result["overall_change"], 0)
        self.assertLess(result["within_effect"], 0)
        self.assertGreater(result["new_course_effect"], 0)
        self.assertEqual([c["course_id"] for c in result["new_courses"]], ["B"])
        # 恒等式：内部+构成+交互+新开课 ≈ 总体变化
        recomposed = (result["within_effect"] + result["composition_effect"]
                      + result["interaction_effect"] + result["new_course_effect"])
        self.assertAlmostEqual(recomposed, result["overall_change"], places=9)
        self.assertAlmostEqual(result["unexplained_residual"], 0.0, places=9)

    def test_pure_composition_change_has_zero_within_effect(self):
        facts = {
            ("A", 1): fact("A", 1, 50, 40),  # 0.8
            ("B", 1): fact("B", 1, 50, 30),  # 0.6
            ("A", 2): fact("A", 2, 90, 72),  # 仍 0.8，份额变大
            ("B", 2): fact("B", 2, 10, 6),   # 仍 0.6，份额变小
        }
        result = decompose(facts, 1, 2)
        self.assertAlmostEqual(result["within_effect"], 0.0, places=9)
        self.assertGreater(result["composition_effect"], 0)  # 高率课份额上升

    def test_suspended_course_is_listed_as_removed_not_counted_as_internal_drop(self):
        facts = {
            ("A", 1): fact("A", 1, 10, 9),
            ("B", 1): fact("B", 1, 10, 8),
            ("A", 2): fact("A", 2, 10, 9),
            ("B", 2): CourseFact("B", 2, expected_sessions=0),  # 停课
        }
        result = decompose(facts, 1, 2)
        self.assertEqual([c["course_id"] for c in result["removed_courses"]], ["B"])
        # A 仍是共同可比课程，但其内部效应为 0，停课的 B 没有被当作内部下降
        self.assertEqual([c["course_id"] for c in result["courses"]], ["A"])
        self.assertAlmostEqual(result["courses"][0]["within"], 0.0, places=9)

    def test_affected_headcount_deduplicates_across_courses(self):
        signals = [
            Signal(course_id="A", period=2, rate=0.5, baseline=0.9, expected_slots=10,
                   affected_students=("S1", "S2"), flagged=True),
            Signal(course_id="B", period=2, rate=0.5, baseline=0.9, expected_slots=10,
                   affected_students=("S2", "S3"), flagged=True),
            Signal(course_id="C", period=2, rate=0.9, baseline=0.9, expected_slots=10,
                   affected_students=("S9",), flagged=False),
        ]
        facts = {("A", 2): fact("A", 2, 10, 5), ("B", 2): fact("B", 2, 10, 5)}
        self.assertEqual(affected_headcount(facts, signals, 2), 3)


if __name__ == "__main__":
    unittest.main()
