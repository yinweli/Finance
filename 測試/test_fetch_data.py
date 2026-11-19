import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "資料"
sys.path.insert(0, str(ROOT / "工具"))

import fetch_data  # noqa: E402
from research_data import prepare  # noqa: E402
from test_research import fixture  # noqa: E402

YTD = {  # 8936 2026 年累計數（仟元、元），取自觀測站彙總表
    1: ("1,715,495", "246,377", "0.99"),
    2: ("3,318,000", "462,871", "1.87"),
    3: ("5,049,811", "655,284", "2.64"),
}


def statement_row(season):
    revenue, net, eps = YTD[season]
    return {"公司代號": "8936", "營業收入": revenue, "淨利（淨損）歸屬於母公司業主": net, "基本每股盈餘（元）": eps}


def tpex_month(row):
    return json.dumps({"tables": [{"data": row}]})


def research(summary):
    """把抓取結果放進固定研究資料，確認能通過正式驗證並計算公式。"""
    data = fixture("8936", "stock", "multiply", ["earnings", "multiple"])
    data.update(as_of="2026-11-19", data_cutoff="2026-11-19T18:00:00+08:00",
                valuation_at="2026-11-18T13:30:00+08:00")
    data["source"] |= summary.source
    data["input"] |= summary.input
    data["formula"] |= summary.formula
    return prepare(data)


class FetchDataTest(unittest.TestCase):
    def setUp(self):
        fetch_data._cache.clear()
        fetch_data._market.clear()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)

    def test_parse_statement(self):
        row = fetch_data.parse_statement((DATA / "t163sb04_otc_115q3.html").read_text(encoding="utf-8"))
        self.assertEqual(row["8936"]["營業收入"], "5,049,811")
        self.assertEqual(row["8936"]["淨利（淨損）歸屬於母公司業主"], "655,284")
        self.assertEqual(row["8936"]["基本每股盈餘（元）"], "2.64")
        self.assertIn("1240", row)

    def test_parse_revenue_with_mixed_case_cell(self):
        html = (DATA / "t21sc03_otc_115_10.html").read_bytes().decode("cp950")
        row = fetch_data.parse_revenue(html, "8936")
        self.assertEqual(len(row), 11)
        self.assertEqual(row[2], "511,266")
        self.assertEqual(row[6], "-7.07")
        self.assertIsNone(fetch_data.parse_revenue(html, "2330"))

    def test_financial_derives_single_quarter(self):
        def statement(typek, year, season):
            return {"8936": statement_row(season)} if typek == "otc" and year == 2026 and season in YTD else {}

        summary = fetch_data.Summary(self.work, "financial", ["8936"])
        with mock.patch.object(fetch_data, "statement", statement):
            fetch_data.financial(summary, ["8936"], 2, date(2026, 11, 19))
        self.assertEqual(summary.input["revenue_2026q1"]["value"], 1715495)
        self.assertEqual(summary.formula["revenue_2026q3"]["args"], ["revenue_ytd_2026q3", "revenue_ytd_2026q2"])
        self.assertEqual(summary.formula["revenue_2026q2"]["args"], ["revenue_ytd_2026q2", "revenue_2026q1"])
        self.assertIn("note", summary.formula["eps_2026q3"])
        result = research(summary)["result"]
        self.assertEqual(result["revenue_2026q3"]["value"], 1731811)
        self.assertEqual(result["net_income_parent_2026q3"]["value"], 192413)

    def test_financial_skips_unpublished_quarter(self):
        def statement(typek, year, season):
            return {"8936": statement_row(season)} if year == 2026 and season in (1, 2) else {}

        summary = fetch_data.Summary(self.work, "financial", ["8936"])
        with mock.patch.object(fetch_data, "statement", statement):
            fetch_data.financial(summary, ["8936"], 1, date(2026, 11, 1))
        self.assertIn("revenue_2026q2", summary.formula)
        self.assertNotIn("revenue_ytd_2026q3", summary.input)
        self.assertEqual(summary.missing, [])

    def test_financial_multiple_ticker_share_source(self):
        def statement(typek, year, season):
            if typek != "otc" or year != 2026 or season not in YTD:
                return {}
            return {"8936": statement_row(season), "1240": statement_row(season) | {"公司代號": "1240"}}

        summary = fetch_data.Summary(self.work, "financial", ["8936", "1240"])
        with mock.patch.object(fetch_data, "statement", statement):
            fetch_data.financial(summary, ["8936", "1240"], 1, date(2026, 11, 19))
        self.assertIn("8936_revenue_2026q3", summary.formula)
        self.assertIn("1240_eps_ytd_2026q3", summary.input)
        summary.save()
        evidence = json.loads((self.work / "來源" / "mops_is_otc_2026q3.json").read_text(encoding="utf-8"))
        self.assertEqual([itor[0] for itor in evidence["rows"]], ["8936", "1240"])

    def test_price_uses_last_trading_day_and_previous_month(self):
        page = {
            ("tradingStock", "2026/09"): [["115/09/29", "1", "1", "52.0", "53.0", "51.0", "52.10"],
                                          ["115/09/30", "1", "1", "52.0", "53.0", "51.0", "52.60"]],
            ("tradingStock", "2026/10"): [["115/10/30", "1", "1", "40.0", "41.0", "39.0", "40.25"]],
            ("peQryStock", "2026/09"): [["115/09/30", "12.12", "5.70", 114, "1.90", "115Q2"]],
            ("peQryStock", "2026/10"): [["115/10/30", "10.30", "7.45", 114, "1.62", "115Q2"]],
        }

        def fetch(url, form=None, encoding="utf-8"):
            kind = "tradingStock" if "tradingStock" in url else "peQryStock"
            month = url.split("date=")[1][:7]
            return tpex_month(page.get((kind, month), []))

        summary = fetch_data.Summary(self.work, "price", ["8936"])
        with mock.patch.object(fetch_data, "fetch", fetch):
            fetch_data.price(summary, "8936", "TPEx", 1, date(2026, 11, 1))
        self.assertEqual(summary.input["close_2026q3"]["value"], 52.6)
        self.assertEqual(summary.input["close_latest"]["value"], 40.25)
        self.assertEqual(summary.input["close_latest"]["date"], "2026-10-30")
        self.assertEqual(summary.input["pe_latest"]["value"], 10.3)
        self.assertIn("115Q2", summary.input["pe_latest"]["note"])
        research(summary)

    def test_price_records_missing_pe_for_etf(self):
        def fetch(url, form=None, encoding="utf-8"):
            if "STOCK_DAY" in url:
                return json.dumps({"stat": "OK", "data": [["115/10/30", "1", "1", "1", "1", "1", "98.50"]]})
            return json.dumps({"stat": "很抱歉，沒有符合條件的資料!"})

        summary = fetch_data.Summary(self.work, "price", ["0050"])
        with mock.patch.object(fetch_data, "fetch", fetch):
            fetch_data.price(summary, "0050", "TWSE", 0, date(2026, 10, 31))
        self.assertEqual(summary.input["close_latest"]["value"], 98.5)
        self.assertNotIn("pe_latest", summary.input)
        self.assertEqual(len(summary.missing), 1)


    def test_yuanta_holding_and_share_per_unit(self):
        page = {
            "PCF": {"trandate": "20261118", "nav": 93.93, "osunit": 25531000000, "totalav": 2398072175479,
                    "upddate": "2026-11-18 16:47:21"},
            "FundWeights": {
                "Summary": {"stkvalues": 2371998714401.0, "futvalues": 4418399200.0, "bndvalues": 0.0},
                "StockWeights": [{"code": "2330", "name": "台積電", "weights": 58.92, "qty": 643681881.0},
                                 {"code": "2454", "name": "聯發科", "weights": 6.42, "qty": 38725084.0}],
                "FutureWeights": [{"code": "TX", "ym": "202612", "name": "臺股期貨", "weights": 0.12, "qty": 358.0}],
                "BondWeights": []},
        }
        summary = fetch_data.Summary(self.work, "etf", ["0050"])
        with mock.patch.object(fetch_data, "fetch", lambda url, form=None, encoding="utf-8": json.dumps(page)):
            fetch_data.etf(summary, "0050", 20)
        self.assertEqual(summary.input["nav"]["value"], 93.93)
        self.assertEqual(summary.input["future_weight_TX202612"]["value"], 0.12)
        self.assertNotIn("bond_value", summary.input)
        result = research(summary)["result"]
        self.assertAlmostEqual(result["shares_per_unit_2330"]["value"], 643681881 / 25531000000)

    def test_yuanta_unknown_ticker(self):
        summary = fetch_data.Summary(self.work, "etf", ["9999"])
        with mock.patch.object(fetch_data, "fetch", lambda url, form=None, encoding="utf-8": "<html></html>"):
            with self.assertRaises(ValueError):
                fetch_data.etf(summary, "9999", 20)

    def test_blackrock_nav_holding_and_group(self):
        nav = "﻿日期,基本面額(A),收益平準金(B)註1,資本損益平準金(C)註2,基金淨值(A+B+C)\n" \
              "2026年11月18日,10.00,0.03,-0.24,9.79\n2026年11月17日,10.00,0.03,-0.23,9.80\n"
        head = "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,Price," \
               "Location,Exchange,Currency,FX Rate,Market Currency"
        holding = "﻿截至,\"2026年11月18日\"\n \n" + head + "\n" + "\n".join([
            '"AAPL","APPLE","資訊科技","指數股票型","1","4.76","1","1","1","美國","NASDAQ","TWD","0.03","USD"',
            '"BRK.B","BERKSHIRE","金融","指數股票型","1","1.20","1","1","1","美國","NYSE","TWD","0.03","USD"',
            '"2330","TSMC","資訊科技","指數股票型","1","1.10","1","1","1","台灣","TWSE","TWD","1.00","TWD"',
            '"JPY","JPY CASH","現金及衍生性商品","Cash","1","-0.02","1","1","1","日本","-","TWD","4.83","JPY"',
        ])

        def fetch(url, form=None, encoding="utf-8"):
            return nav if "historical-nav" in url else holding

        summary = fetch_data.Summary(self.work, "etf", ["009826"])
        with mock.patch.object(fetch_data, "fetch", fetch):
            fetch_data.etf(summary, "009826", 2)
        self.assertEqual(summary.input["nav"]["value"], 9.79)
        self.assertEqual(summary.input["nav"]["date"], "2026-11-18")
        self.assertEqual(list(summary.input), ["nav", "holding_weight_AAPL", "holding_weight_BRK-B"])
        self.assertEqual(summary.table["location"][0], ["美國", 5.96])
        self.assertIn(["日本", -0.02], summary.table["location"])
        research(summary)


if __name__ == "__main__":
    unittest.main()
