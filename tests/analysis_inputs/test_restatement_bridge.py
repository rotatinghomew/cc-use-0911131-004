"""重述分析（restatement）回归测试。

覆盖协会质疑所需的分辨能力：

* 价格没变但样本换了：headline 上升全部来自样本更替，matched_price 为 0；
* 迟到更正：原发布之后收到的 revision 只进入重述版，差异归入 late_revision，
  原发布与链式历史不被覆盖；
* 断档/映射冲突：明确 not_comparable，不生成平滑增长率；
* 零价格/缺报价：排除而非按 0 计权；
* 对账：所有桥接贡献之和等于总变动，不存在“其他”残差；
* 双时态与可追溯：贡献可追到原报价 observation_id、revision 与映射版本。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from market_index_bureau.restatement.domain import RestatementError
from market_index_bureau.restatement.service import analyze

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "fixtures" / "analysis"
MAIN_FIXTURE = FIXTURE_ROOT / "input.json"
REGRESSION_DIR = FIXTURE_ROOT / "regressions"


class RestatementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "restatement.sqlite3"

    def run_analysis(self, fixture: Path) -> dict:
        return analyze(fixture, self.database)

    def restated_vintage(self, result: dict) -> dict:
        return next(item for item in result["vintages"] if item["kind"] == "restatement")

    def original_vintage(self, result: dict, as_of: str) -> dict:
        return next(
            item
            for item in result["vintages"]
            if item["kind"] == "original" and item["as_of"] == as_of
        )

    def period(self, vintage: dict, period: str) -> dict:
        return next(item for item in vintage["periods"] if item["period"] == period)

    def period_contributions(self, result: dict, vintage_as_of: str, period: str) -> list[dict]:
        return [
            item
            for item in result["contributions"]
            if item["vintage_as_of"] == vintage_as_of and item["period"] == period
        ]


class MainFixtureTests(RestatementTestCase):
    def test_output_contract_has_required_sections(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        for key in (
            "publication_id",
            "vintages",
            "contributions",
            "bridge_status",
            "reconciliation",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["publication_id"], "published-march")
        # 每个 vintage 的每个期间都给出可比状态与所用来源版本。
        for vintage in result["vintages"]:
            self.assertIn("source_versions", vintage)
            self.assertTrue(vintage["periods"])
            for period in vintage["periods"]:
                self.assertIn("comparable", period)
                self.assertIn("basis", period)
        for rows in result["bridge_status"].values():
            self.assertTrue(rows)

    def test_vintages_reflect_bitemporal_knowledge(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        original = self.original_vintage(result, "2026-03-31")
        restated = self.restated_vintage(result)
        # 原发布时点只能看到 2026-01..03 三期。
        self.assertEqual([item["period"] for item in original["periods"]],
                         ["2026-01", "2026-02", "2026-03"])
        # 重述版覆盖到 2026-06，且四月起按新口径（类别映射生效）。
        restated_periods = [item["period"] for item in restated["periods"]]
        self.assertEqual(restated_periods,
                         ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"])
        self.assertEqual(self.period(restated, "2026-03")["basis"], "old")
        self.assertEqual(self.period(restated, "2026-04")["basis"], "new")
        # 锚定原发布水平 100，链式历史接续而非重置。
        self.assertEqual(self.period(restated, "2026-03")["matched_index"], 100.0)

    def test_out_of_panel_merchants_are_excluded_before_entry(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        restated = self.restated_vintage(result)
        # m10-m13 四月才入样：一月篮子只有 10 户，四月有 14 户。
        self.assertEqual(self.period(restated, "2026-01")["basket_size"], 10)
        self.assertEqual(self.period(restated, "2026-04")["basket_size"], 14)
        self.assertEqual(self.period(restated, "2026-04")["entrant_count"], 4)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("out_of_panel_quote", codes)
        # 提前报送的报价仅留存证据，不进任何篮子：来源版本记录里不含
        # m10-m13 的一月报价 p1-10..p1-13。
        self.assertNotIn(
            "p1-10", restated["source_versions"]["observation_versions"]
        )

    def test_april_jump_is_mostly_sample_replacement_not_price(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        restated = self.restated_vintage(result)
        contributions = {
            item["factor"]: item["amount"]
            for item in self.period_contributions(result, "2026-07-15", "2026-04")
        }
        # 纯价格贡献约 0.33 个指数点，样本进入贡献远大于它。
        self.assertAlmostEqual(contributions["matched_price"], 0.325541, places=4)
        self.assertGreater(contributions["sample_entry"], 30.0)
        # 类别纯改名迁移贡献为 0（逐键留痕但不改变总水平）。
        self.assertEqual(contributions["category_migration"], 0.0)
        # 匹配链只反映存续样本的价格变化：四月相对三月仅约 +0.6%。
        april = self.period(restated, "2026-04")
        self.assertAlmostEqual(april["matched_index"], 100.602347, places=4)
        self.assertTrue(april["comparable"])

    def test_all_bridges_reconcile_without_other_residual(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        self.assertTrue(result["reconciliation"]["all_balanced"])
        for rows in result["reconciliation"]["period_bridges"].values():
            for row in rows:
                self.assertTrue(row["balanced"], row)
                self.assertLessEqual(abs(row["residual"]), row["tolerance"])
        for row in result["reconciliation"]["publication_checks"]:
            self.assertTrue(row["balanced"])
            self.assertEqual(row["recomputed_matched_index"], row["published_level"])
        # 贡献因子只允许固定集合，禁止“其他”兜底。
        allowed = {
            "matched_price", "sample_entry", "sample_exit", "weight_mix",
            "category_migration", "late_revision",
        }
        for item in result["contributions"]:
            self.assertIn(item["factor"], allowed)

    def test_contributions_trace_to_observations_and_mapping_versions(self) -> None:
        result = self.run_analysis(MAIN_FIXTURE)
        entry = next(
            item
            for item in result["contributions"]
            if item["period"] == "2026-04" and item["factor"] == "sample_entry"
        )
        self.assertEqual(
            sorted(entry["observation_ids"]),
            ["p4-10", "p4-11", "p4-12", "p4-13"],
        )
        for detail in entry["evidence"]["items"]:
            self.assertIn("merchant_id", detail)
            self.assertIn("revisions", detail)
        restated = self.restated_vintage(result)
        versions = restated["source_versions"]["observation_versions"]
        self.assertTrue(versions)
        self.assertIn("map-a", restated["source_versions"]["mapping_ids"])


class SampleReplacementRegressionTests(RestatementTestCase):
    FIXTURE = REGRESSION_DIR / "sample_replacement_no_price_change.json"

    def test_headline_rise_but_zero_matched_price_change(self) -> None:
        result = self.run_analysis(self.FIXTURE)
        restated = self.restated_vintage(result)
        january = self.period(restated, "2026-01")
        february = self.period(restated, "2026-02")
        # 存续商户价格完全没变：匹配链保持在锚点 100。
        self.assertEqual(february["matched_index"], 100.0)
        self.assertTrue(february["comparable"])
        # headline 水平因高价新入样商户而上升。
        self.assertGreater(february["headline_level"], january["headline_level"])
        contributions = {
            item["factor"]: item["amount"]
            for item in self.period_contributions(result, "2026-03-20", "2026-02")
        }
        self.assertEqual(contributions["matched_price"], 0.0)
        self.assertGreater(contributions["sample_entry"], 0.0)
        # 经济变化（0）与口径/样本变化（非 0）被明确分开，且对账闭合。
        bridge = next(
            row for row in result["reconciliation"]["period_bridges"]["2026-03-20"]
            if row["period"] == "2026-02"
        )
        self.assertTrue(bridge["balanced"])
        self.assertEqual(bridge["total_change"],
                         february["headline_level"] - january["headline_level"])


class LateCorrectionRegressionTests(RestatementTestCase):
    FIXTURE = REGRESSION_DIR / "late_correction_after_publication.json"

    def test_late_revision_creates_restatement_without_overwriting(self) -> None:
        result = self.run_analysis(self.FIXTURE)
        original = self.original_vintage(result, "2026-03-31")
        restated = self.restated_vintage(result)
        # 原发布只知道 revision 1，重述版使用 revision 2。
        self.assertEqual(
            original["source_versions"]["observation_versions"]["r3-m0"], 1
        )
        self.assertEqual(
            restated["source_versions"]["observation_versions"]["r3-m0"], 2
        )
        original_march = self.period(original, "2026-03")["headline_level"]
        restated_march = self.period(restated, "2026-03")["headline_level"]
        self.assertGreater(restated_march, original_march)
        # 差异全部由 late_revision 解释，无残差。
        delta = next(
            item
            for item in result["contributions"]
            if item["vintage_as_of"] == "restatement-vs:2026-03-31"
            and item["period"] == "2026-03"
            and item["factor"] == "late_revision"
        )
        self.assertAlmostEqual(
            delta["amount"], restated_march - original_march, places=9
        )
        self.assertEqual(delta["observation_ids"], ["r3-m0"])
        check = next(
            row for row in result["reconciliation"]["vintage_restatements"]
            if row["period"] == "2026-03"
        )
        self.assertTrue(check["balanced"])
        # 同一 observation_id 的两个 revision 不会被重复计权。
        restated_march_quotes = [
            item for item in self.period(restated, "2026-03").get("flags", [])
        ]
        self.assertNotIn("duplicate_weight", restated_march_quotes)

    def test_original_publication_rows_are_append_only_in_storage(self) -> None:
        self.run_analysis(self.FIXTURE)
        with sqlite3.connect(self.database) as connection:
            count_first = connection.execute(
                "SELECT COUNT(*) FROM restatement_vintages WHERE kind='original'"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT vintage_key, as_of, anchor_level FROM restatement_vintages "
                "WHERE kind='original'"
            ).fetchall()
        self.assertEqual(count_first, 1)
        self.assertEqual(rows[0][1], "2026-03-31")
        self.assertEqual(rows[0][2], 100.0)
        # 再跑一次：幂等重放，不新增、不覆盖历史行。
        second = self.run_analysis(self.FIXTURE)
        self.assertTrue(second["replayed"])
        with sqlite3.connect(self.database) as connection:
            count_second = connection.execute(
                "SELECT COUNT(*) FROM restatement_vintages WHERE kind='original'"
            ).fetchone()[0]
        self.assertEqual(count_first, count_second)


class GapConflictInvalidPriceTests(RestatementTestCase):
    FIXTURE = REGRESSION_DIR / "gap_conflict_invalid_price.json"

    def test_coverage_gap_is_not_smoothed(self) -> None:
        result = self.run_analysis(self.FIXTURE)
        restated = self.restated_vintage(result)
        february = self.period(restated, "2026-02")
        self.assertFalse(february["comparable"])
        self.assertIn("not_comparable_gap", february["flags"])
        # 沿用上一可比水平，而不是编造增长率。
        self.assertEqual(february["matched_index"], 100.0)
        status = next(
            row for row in result["bridge_status"]["2026-04-20"]
            if row["period"] == "2026-02"
        )
        self.assertEqual(status["status"], "not_comparable")

    def test_mapping_conflict_is_flagged_incomparable(self) -> None:
        result = self.run_analysis(self.FIXTURE)
        restated = self.restated_vintage(result)
        march = self.period(restated, "2026-03")
        self.assertIn("not_comparable_mapping_conflict", march["flags"])
        issue = next(
            item for item in result["issues"] if item["code"] == "mapping_conflict"
        )
        self.assertEqual(set(issue["mapping_ids"]), {"map-y-1", "map-y-2"})

    def test_zero_price_is_excluded_not_counted_as_zero(self) -> None:
        result = self.run_analysis(self.FIXTURE)
        restated = self.restated_vintage(result)
        march = self.period(restated, "2026-03")
        # m2 的零价格被排除：三月篮子只有 m0、m1 两户有效报价。
        self.assertEqual(march["basket_size"], 2)
        issue = next(
            item for item in result["issues"]
            if item["code"] == "invalid_price_excluded"
        )
        self.assertEqual(issue["observation_ids"], ["r3-m2"])


class AnomalyHandlingTests(RestatementTestCase):
    FIXTURE = REGRESSION_DIR / "sample_replacement_no_price_change.json"

    def test_extreme_quote_is_flagged_but_retained(self) -> None:
        payload = json.loads(self.FIXTURE.read_text("utf-8"))
        for observation in payload["observations"]:
            if observation["observation_id"] == "r2-m0":
                observation["price"] = 95.0  # 正常 10，极端偏离
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anomaly.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_analysis(path)
        anomaly = next(
            item for item in result["issues"] if item["code"] == "anomaly_price"
        )
        self.assertEqual(anomaly["observation_ids"], ["r2-m0"])
        restated = self.restated_vintage(result)
        february = self.period(restated, "2026-02")
        # 异常报价保留在篮子里（3 户），只是被标记，绝不静默删除或当零。
        self.assertEqual(february["basket_size"], 3)


class EmptyBasketTests(RestatementTestCase):
    FIXTURE = REGRESSION_DIR / "sample_replacement_no_price_change.json"

    def test_period_with_no_valid_quotes_has_no_fabricated_level(self) -> None:
        payload = json.loads(self.FIXTURE.read_text("utf-8"))
        # 二月所有报价全部置为非正价格：二月篮子应为空（含新入样的 r2-m2）。
        for observation in payload["observations"]:
            if observation["period"] == "2026-02":
                observation["price"] = -1.0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_analysis(path)
        restated = self.restated_vintage(result)
        february = self.period(restated, "2026-02")
        self.assertEqual(february["basket_size"], 0)
        self.assertIsNone(february["headline_level"])
        self.assertFalse(february["comparable"])
        self.assertIn("empty_basket", february["flags"])
        bridge = next(
            row for row in result["bridge_status"][result["as_of"]]
            if row["period"] == "2026-02"
        )
        self.assertEqual(bridge["status"], "not_comparable")


class FeedContractTests(RestatementTestCase):
    def test_duplicate_revision_is_rejected(self) -> None:
        payload = json.loads(Path(REGRESSION_DIR / "late_correction_after_publication.json").read_text("utf-8"))
        duplicate = dict(payload["observations"][2])  # r3-m0 revision 1
        payload["observations"].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RestatementError):
                self.run_analysis(path)

    def test_missing_unit_conversion_is_rejected(self) -> None:
        payload = json.loads(MAIN_FIXTURE.read_text("utf-8"))
        payload["unit_conversions"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RestatementError):
                self.run_analysis(path)

    def test_non_positive_quantity_is_rejected(self) -> None:
        payload = json.loads(MAIN_FIXTURE.read_text("utf-8"))
        payload["observations"][0]["quantity"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RestatementError):
                self.run_analysis(path)


if __name__ == "__main__":
    unittest.main()
