import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "工具" / "research_report.py"


def fixture(code, asset_type, operation, argument):
    source = "sample"
    def observation(value, unit):
        return {"value": value, "unit": unit, "kind": "verified",
                "source": [source], "date": "2026-01-09"}

    return {
        "schema_version": 1, "subject": f"{code}加碼研究", "ticker": code,
        "exchange": "TWSE" if asset_type == "equity_etf" else "TPEx",
        "asset_type": asset_type, "as_of": "2026-01-10",
        "valuation_at": "2026-01-09T13:30:00+08:00",
        "data_cutoff": "2026-01-10T18:00:00+08:00",
        "method": {"mode": "finance", "detail": "固定測試資料"},
        "source": {source: {"name": "固定測試資料", "url": "https://example.org/fixture",
                            "data_date": "2026-01-09"}},
        "input": {"price": observation(100, "元"), "nav": observation(100, "元"),
                  "cash": observation(2, "元"), "earnings": observation(5, "元")},
        "assumption": {"multiple": {"value": 20, "unit": "倍", "kind": "assumption",
                                    "note": "僅供測試", "date": "2026-01-10"}},
        "formula": {
            "fair": {"op": operation, "args": argument, "unit": "元"},
            "premium": {"op": "premium", "args": ["price", "nav"], "unit": "%"},
            "bear": {"op": "subtract", "args": ["fair", 10], "unit": "元"},
            "bull": {"op": "add", "args": ["fair", 10], "unit": "元"},
            **{f"total_{key}": {"op": "total_return", "args": ["price", target, "cash"], "unit": "%"}
               for key, target in (("bear", "bear"), ("base", "fair"), ("bull", "bull"))},
        },
        "valuation": {"fair": "fair", "as_of_price": "price", "method": "固定模型",
                      "cross_check": "備援方法僅供測試"},
        "scenarios": {key: {"price": target, "distribution": "cash", "return": f"total_{key}",
                            "summary": key, "end_date": "2027-01-10"}
                      for key, target in (("bear", "bear"), ("base", "fair"), ("bull", "bull"))},
        "conclusion": "合理價 [[fair]]，現價 [[price]]",
        "metrics": {"市價": "price", "合理價": "fair"},
        "thesis": {"view": "測試論點", "counter": "測試反證"},
        "events": [{"date": "2026-01-09", "title": "測試事件", "status": "verified", "source": [source]}],
        "review": [{"trigger": "行情變動", "action": "重算折溢價及報酬"}],
        "sections": {"data": [{"type": "table", "title": "成分與配息", "columns": ["項目", "數字"],
                               "rows": [["配息", "[[cash]]"]], "source": [source]}],
                     "analysis": [{"type": "chart", "title": "估值與情境", "unit": "元",
                                   "period": "2026 年", "source": [source],
                                   "labels": ["保守", "中性", "樂觀"], "style": "bar",
                                   "series": {"價格": ["bear", "fair", "bull"]}}],
                     "scenarios": [{"type": "text", "title": "敏感度", "text": "折溢價 [[premium]]"}]},
        "limits": ["本資料為固定測試用"],
    }


class ResearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, data, name="input.json"):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(self, *arg, success=True):
        result = subprocess.run([sys.executable, "-X", "utf8", str(CLI), *map(str, arg)],
                                capture_output=True, encoding="utf-8")
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def render(self, data):
        return Path(self.invoke("render", self.put(data), "--output-dir", self.root).stdout.strip())

    def extract(self, path):
        return json.loads(self.invoke("extract", path).stdout)

    def test_equity_etf_report_and_incremental_version(self):
        data = fixture("0050", "equity_etf", "basket_value", ["earnings", "multiple", 1])
        data["input"]["nav"]["value"] = 99
        data["input"]["nav"]["precision"] = 4
        data["input"]["nav"]["observed_at"] = "2026-01-09T19:30:00+08:00"
        for key, value in (("major_eps", 1), ("other_eps", 2.5), ("loss_eps", -3)):
            data["input"][key] = data["input"]["earnings"] | {"value": value, "unit": "元／股"}
        data["formula"]["basket_eps"] = {
            "op": "basket_earnings", "args": [3, "major_eps", 2, "other_eps", 1, "loss_eps"], "unit": "元",
        }
        data["formula"]["fair"]["args"][0] = "basket_eps"
        data["sections"]["data"][0]["rows"] += [["整籃每單位獲利", "[[basket_eps]]"]]
        data["sections"]["analysis"].append({
            "type": "chart", "title": "估值區間", "unit": "元", "period": "2026 年",
            "source": ["sample"], "labels": ["合理區間"], "style": "range",
            "series": {"估值": [["bear", "bull"]]}, "reference": "price",
        })
        original = self.render(data)
        html = original.read_text(encoding="utf-8")
        self.assertIn("101.00 元", html)
        self.assertIn("99.0000 元", html)
        self.assertIn("2026-01-09 19:30:00+08:00", html)
        self.assertIn("data-value=\"101.0\"", html)
        self.assertIn("<details", html)
        self.assertEqual(self.extract(original)["result"]["fair"]["value"], 101)
        self.assertEqual(self.extract(original)["result"]["basket_eps"]["value"], 5)
        self.assertEqual(self.extract(original)["result"]["case_base_price_change"]["value"], 1)
        patch = {"as_of": data["as_of"], "data_cutoff": "2026-01-10T19:00:00+08:00",
                 "input": {"price": {"value": 98}},
                 "update": {"changes": ["更新市價"], "thesis": "unchanged", "reason": "估值輸入相同"}}
        updated = Path(self.invoke("update", self.put(patch), "--output-dir", self.root,
                                   "--ticker", "0050", "--exchange", "TWSE",
                                   "--subject", data["subject"]).stdout.strip())
        self.assertTrue(updated.name.endswith("-v2.html"))
        self.assertEqual(original.read_text(encoding="utf-8"), html)
        snapshot = self.extract(updated)
        self.assertEqual(snapshot["sections"], data["sections"])
        self.assertEqual(snapshot["update"]["prior_report"], original.name)
        self.assertAlmostEqual(snapshot["result"]["premium"]["value"], 100 * (98 / 99 - 1))
        self.assertAlmostEqual(snapshot["result"]["total_base"]["value"], 100 * (103 / 98 - 1))
        selected = json.loads(self.invoke("extract", updated, "--field", "input.price").stdout)
        self.assertEqual(list(selected), ["input.price"])

    def test_company_quarter_model_and_material_update(self):
        data = fixture("8936", "stock", "multiply", ["earnings", "multiple"])
        data["method"] = {"mode": "plugin", "skill": "earnings-deep-dive", "version": "0.1.31"}
        data["formula"]["revenue_q"] = {"op": "add", "args": [90, 10], "unit": "百萬元"}
        data["formula"]["receipts_q"] = {"op": "add", "args": [70, 10], "unit": "百萬元"}
        data["formula"]["cash_q"] = {"op": "cash_flow", "args": ["receipts_q", 65, 5], "unit": "百萬元"}
        label = [f"{year}Q{quarter}" for year in range(2023, 2030) for quarter in range(1, 5)]
        for index, period in enumerate(label):
            if index < 12:
                data["input"][period] = data["input"]["earnings"] | {"value": 70 + index, "unit": "百萬元"}
            else:
                data["formula"][period] = {"op": "add", "args": ["revenue_q", index - 12], "unit": "百萬元"}
        data["sections"]["analysis"].append({
            "type": "chart", "title": "單季工程營收與事件", "unit": "百萬元", "period": "2023Q1–2029Q4",
            "source": ["sample"], "labels": label, "style": "line",
            "series": {"季度營收": label}, "forecast_from": label[12],
            "events": [{"at": label[12], "index": 0}],
        })
        data["formula"]["revenue_2026"] = {"op": "sum", "args": label[12:16], "unit": "百萬元"}
        data["formula"]["eps_q"] = {"op": "eps", "args": [10, 2], "unit": "元"}
        data["formula"]["four_year_return"] = {
            "op": "annualized_return", "args": ["price", "bull", 4, 4], "unit": "%",
        }
        data["sections"]["data"][0]["rows"] += [["工程收入", "[[revenue_q]]"], ["現金流", "[[cash_q]]"]]
        path = self.render(data)
        self.assertEqual(self.extract(path)["result"]["cash_q"]["value"], 10)
        self.assertEqual(self.extract(path)["result"]["revenue_2026"]["value"], 406)
        self.assertEqual(self.extract(path)["result"]["eps_q"]["value"], 5)
        self.assertIn("2029Q4", path.read_text(encoding="utf-8"))
        patch = {"as_of": "2026-01-11", "data_cutoff": "2026-01-11T18:00:00+08:00",
                 "formula": {"receipts_q": {"args": [60, 10]}},
                 "update": {"changes": ["回款延後"], "thesis": "revised", "reason": "回款延後"},
                 "thesis": {"view": "回款風險上升"}}
        updated = Path(self.invoke("update", self.put(patch), "--base", path,
                                   "--output-dir", self.root).stdout.strip())
        self.assertEqual(self.extract(updated)["result"]["cash_q"]["value"], 0)
        self.assertIn("回款風險上升", updated.read_text(encoding="utf-8"))

    def test_bond_duration_convexity_and_fx_avoid_equity_route(self):
        data = fixture("00679B", "bond_etf", "bond_value", ["nav", 15, 300, 0.0025, 33, 32, 0.9])
        data["formula"]["fx_down_yield_up"] = {"op": "bond_value",
                                              "args": ["nav", 15, 300, .0025, 31, 32, .9], "unit": "元"}
        data["sections"]["scenarios"].append({
            "type": "table", "title": "利率 × 匯率", "columns": ["長端", "美元升", "美元跌"],
            "rows": [["升 25 基點", "[[fair]]", "[[fx_down_yield_up]]"]], "source": ["sample"],
        })
        path = self.render(data)
        expected = 100 * (1 - 15 * .0025 + 150 * .0025**2) * (1 + .9 * (33 / 32 - 1))
        self.assertAlmostEqual(self.extract(path)["result"]["fair"]["value"], expected)
        self.assertLess(self.extract(path)["result"]["fx_down_yield_up"]["value"], expected)
        data["method"] = {"mode": "plugin", "skill": "initiating-coverage", "version": "0.1.31"}
        self.assertIn("bond_etf", self.invoke("render", self.put(data), success=False).stderr)
        data["asset_type"] = "stock"
        self.assertIn("bond_etf", self.invoke("render", self.put(data), success=False).stderr)

    def test_snapshot_compatibility_and_invalid_inputs(self):
        legacy = self.root / "legacy.html"
        legacy.write_text('<script type="application/json" id="research-data">'
                          '{"subject":"0050","as_of":"2026-01-01","input":{"x":1}}'
                          '</script>', encoding="utf-8")
        self.assertEqual(self.extract(legacy)["input"], {"x": 1})
        data = fixture("0050", "equity_etf", "basket_value", ["earnings", "multiple", 1])
        invalid = deepcopy(data)
        invalid["source"]["sample"]["url"] = "javascript:alert(1)"
        self.assertIn("http", self.invoke("render", self.put(invalid), success=False).stderr)
        invalid = deepcopy(data)
        invalid["input"]["price"]["source"] = ["missing"]
        self.assertIn("missing", self.invoke("render", self.put(invalid), success=False).stderr)
        data["limits"] = ["</script><script src=https://example.org/code>"]
        path = self.render(data)
        self.invoke("verify", path)
        self.assertNotIn("<script src=", path.read_text(encoding="utf-8"))

    def test_scenario_return_and_tampered_html(self):
        data = fixture("0050", "equity_etf", "basket_value", ["earnings", "multiple", 1])
        bad = deepcopy(data)
        bad["scenarios"]["base"]["return"] = "total_bear"
        self.assertIn("含息報酬", self.invoke("render", self.put(bad), success=False).stderr)
        path = self.render(data)
        markup = path.read_text(encoding="utf-8").replace("合理價 101.00", "合理價 901.00", 1)
        path.write_text(markup, encoding="utf-8")
        self.invoke("verify", path, success=False)

    def test_missing_latest_and_personal_report(self):
        self.assertIn("首次研究", self.invoke("latest", "--output-dir", self.root,
                                             "--ticker", "0050", "--exchange", "TWSE",
                                             "--subject", "0050加碼研究", success=False).stderr)
        data = fixture("開銷", "spending", "sum", [80, 20])
        for key in ("valuation", "scenarios", "thesis", "ticker", "exchange", "valuation_at"):
            del data[key]
        data["source"]["sample"].pop("url")
        data["source"]["sample"]["locator"] = "去識別化來源"
        path = self.render(data)
        self.invoke("verify", path)
        data["personal_holdings"] = {}
        self.assertIn("個人財務欄位", self.invoke("render", self.put(data), success=False).stderr)


if __name__ == "__main__":
    unittest.main()
