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
import fetch_market  # noqa: E402
import source  # noqa: E402

YTD = {  # 8936 2026 年累計數（仟元、元），取自觀測站彙總表
    1: ("1,715,495", "246,377", "0.99"),
    2: ("3,318,000", "462,871", "1.87"),
    3: ("5,049,811", "655,284", "2.64"),
}


def statement_row(season):
    revenue, net, eps = YTD[season]
    return {"公司代號": "8936", "營業收入": revenue, "營業毛利（毛損）": "100", "淨利（淨損）歸屬於母公司業主": net,
            "基本每股盈餘（元）": eps}


class FetchDataTest(unittest.TestCase):
    def setUp(self):
        fetch_data._market.clear()
        source._memory.clear()

    def test_parse_statement(self):
        row = fetch_data.parse_statement((DATA / "t163sb04_otc_115q3.html").read_text(encoding="utf-8"))
        self.assertEqual(row["8936"]["營業收入"], "5,049,811")
        self.assertEqual(row["8936"]["基本每股盈餘（元）"], "2.64")
        self.assertIn("1240", row)

    def test_parse_revenue_with_mixed_case_cell(self):
        html = (DATA / "t21sc03_otc_115_10.html").read_bytes().decode("cp950")
        row = fetch_data.parse_revenue(html, "8936")
        self.assertEqual(row[2], "511,266")
        self.assertEqual(row[6], "-7.07")
        self.assertIsNone(fetch_data.parse_revenue(html, "2330"))

    def test_financial_derives_single_quarter(self):
        def statement(typek, year, season):
            return {"8936": statement_row(season)} if typek == "otc" and year == 2026 and season in YTD else {}

        with mock.patch.object(fetch_data, "statement", statement):
            data = fetch_data.financial("8936", 2, date(2026, 11, 19))
        self.assertEqual([r[0] for r in data.row], ["2026Q2", "2026Q3"])
        q3 = data.row[-1]
        self.assertEqual(q3[1], 1731811)
        self.assertEqual(q3[5], 192413)
        self.assertAlmostEqual(q3[6], 0.77)
        self.assertEqual(data.missing, [])

    def test_financial_skips_unpublished_quarter(self):
        def statement(typek, year, season):
            return {"8936": statement_row(season)} if year == 2026 and season in (1, 2) else {}

        with mock.patch.object(fetch_data, "statement", statement):
            data = fetch_data.financial("8936", 1, date(2026, 11, 1))
        self.assertEqual([r[0] for r in data.row], ["2026Q2"])
        self.assertIn("2026Q3 財報尚未公告", data.missing)

    def test_daily_converts_twse_shares_to_lots(self):
        twse = json.dumps({"stat": "OK", "data": [["115/11/02", "160,932,537", "0", "103.40", "104.45", "103.05", "104.10"]]})
        tpex = json.dumps({"tables": [{"data": [["115/11/02", "768", "0", "44.20", "44.65", "43.60", "44.15"]]}]})
        with mock.patch.object(fetch_data, "fetch", lambda url, **_: twse):
            row = fetch_data.daily("0050", "TWSE", 1, date(2026, 11, 19)).row
        self.assertEqual(row, [["2026-11-02", 103.4, 104.45, 103.05, 104.1, 160933]])
        with mock.patch.object(fetch_data, "fetch", lambda url, **_: tpex):
            row = fetch_data.daily("8936", "TPEx", 1, date(2026, 11, 19)).row
        self.assertEqual(row[0][5], 768)

    def test_valuation_column_order(self):
        twse = json.dumps({"stat": "OK", "data": [["115年11月02日", "0.94", 114, "27.01", "9.39", "115/2"]]})
        tpex = json.dumps({"tables": [{"data": [["115/11/02", "10.17", "6.80", 114, "1.73", "115Q2"]]}]})
        with mock.patch.object(fetch_data, "fetch", lambda url, **_: twse):
            self.assertEqual(fetch_data.valuation("2330", "TWSE", 1, date(2026, 11, 19)).row[0],
                             ["2026-11-02", 27.01, 9.39, 0.94, "115/2"])
        with mock.patch.object(fetch_data, "fetch", lambda url, **_: tpex):
            self.assertEqual(fetch_data.valuation("8936", "TPEx", 1, date(2026, 11, 19)).row[0],
                             ["2026-11-02", 10.17, 1.73, 6.8, "115Q2"])

    def test_yuanta_holding(self):
        data = {"PCF": {"nav": 90.05, "trandate": "20261118", "osunit": 100, "totalav": 9005, "upddate": "x"},
                "FundWeights": {"StockWeights": [{"code": "2317", "name": "鴻海", "weights": 5.1, "qty": 10},
                                                 {"code": "2330", "name": "台積電", "weights": 55.2, "qty": 20}],
                                "FutureWeights": [{"code": "TXF", "ym": "202612", "name": "台指期", "weights": 0.5, "qty": 1}]}}
        with mock.patch.object(fetch_data, "fetch_json", lambda url: data):
            nav, holding = fetch_data.etf("0050")
        self.assertEqual(nav.row, [["2026-11-18", 90.05, 100, 9005]])
        self.assertEqual([r[1] for r in holding.row], ["2330", "2317", "TXF202612"])

    def test_yuanta_unknown_ticker(self):
        with mock.patch.object(fetch_data, "fetch_json", lambda url: {}):
            with self.assertRaises(ValueError):
                fetch_data.etf("9999")


class SplitTest(unittest.TestCase):
    def test_adjust_split_restates_earlier_rows(self):
        import collect
        price = source.Dataset("price", "p", [])
        price.row = [["2025-06-09", 190, 191, 188, 189.0, 1000], ["2025-06-10", 188, 189, 187, 188.65, 1000],
                     ["2025-06-18", 47, 48, 47, 47.57, 4000]]
        dividend = source.Dataset("dividend", "d", [])
        dividend.row = [["2025-01-20", 2.7, "除息"], ["2025-07-21", 0.36, "除息"]]
        self.assertEqual(collect.adjust_split(price, dividend), [("2025-06-18", 4)])
        self.assertAlmostEqual(price.row[1][4], 47.1625)
        self.assertEqual(price.row[1][5], 4000)
        self.assertEqual(price.row[2][4], 47.57)
        self.assertEqual([r[1] for r in dividend.row], [0.675, 0.36])
        self.assertIn("1 拆 4", price.note)

    def test_normal_limit_move_is_not_split(self):
        import collect
        price = source.Dataset("price", "p", [])
        price.row = [["2026-11-05", 1, 1, 1, 44.0, 1], ["2026-11-06", 1, 1, 1, 39.6, 1]]
        self.assertEqual(collect.adjust_split(price), [])


class FetchMarketTest(unittest.TestCase):
    def setUp(self):
        source._memory.clear()

    def test_institutional_twse_and_tpex_columns(self):
        twse = {"stat": "OK", "data": [["2330", "台積電", "0", "0", "1,000,000", "0", "0", "2,000", "0", "0", "-3,000",
                                        "5,000", "0", "0", "0", "0", "0", "0", "1,004,000"]]}
        row = ["8936", "國統"] + ["0"] * 21 + ["9,000"]
        row[10], row[13], row[22] = "4,000", "3,000", "2,000"
        tpex = {"tables": [{"data": [row]}]}
        day = [date(2026, 11, 18)]
        with mock.patch.object(fetch_market, "market_json", lambda url, d, t: twse):
            got = fetch_market.institutional("2330", "TWSE", day, date(2026, 11, 19)).row
        self.assertEqual(got, [["2026-11-18", 1002, -3, 5, 1004]])
        with mock.patch.object(fetch_market, "market_json", lambda url, d, t: tpex):
            got = fetch_market.institutional("8936", "TPEx", day, date(2026, 11, 19)).row
        self.assertEqual(got, [["2026-11-18", 4, 3, 2, 9]])

    def test_holders_accumulates_weekly_history(self):
        text = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n" + "\n".join(
            f"20261113,8936,{level},{100 if level == 17 else 1},0,{pct}"
            for level, pct in ((1, 5.0), (2, 4.0), (3, 1.0), (12, 10.0), (15, 30.0), (17, 100.0)))
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(fetch_market, "CACHE", Path(folder)), \
                mock.patch.object(fetch_market, "fetch", lambda url: text):
            history = Path(folder) / "集保" / "8936.json"
            history.parent.mkdir()
            history.write_text(json.dumps({"2026-11-06": [39.0, 29.0, 10.5, 98]}), encoding="utf-8")
            row = fetch_market.holders("8936").row
        self.assertEqual(row, [["2026-11-06", 39.0, 29.0, 10.5, 98], ["2026-11-13", 40.0, 30.0, 10.0, 100]])

    def test_ust_keeps_two_years(self):
        text = 'Date,"1 Mo","2 Yr","10 Yr","20 Yr","30 Yr"\n11/18/2026,4.1,4.80,5.07,5.44,5.41\n01/02/2024,5,4,4,4,4\n'
        with mock.patch.object(fetch_market, "fetch", lambda url, keep=False: text):
            row = fetch_market.ust(2, date(2026, 11, 19)).row
        self.assertEqual(row[-1], ["2026-11-18", 4.8, 5.07, 5.44, 5.41])
        self.assertNotIn("2024-01-02", [r[0] for r in row])

    def test_us_index_falls_back_to_fred(self):
        # 2026-11-19、11-20 美東 9:30 開盤的時間戳記；gmtoffset 換算後應是美國當地日期
        chart = json.dumps({"chart": {"result": [{"meta": {"gmtoffset": -18000},
                                                   "timestamp": [1795098600, 1795185000],
                                                   "indicators": {"quote": [{"close": [100.123, None]}]}}]}})

        def fake(url, keep=False):
            if "%5EGSPC" in url:
                raise source.HTTPError(url, 429, "Too Many Requests", None, None)
            if "fred" in url:
                return "observation_date,SP500\n2026-11-19,7175.83\n2026-11-20,.\n"
            return chart

        with mock.patch.object(fetch_market, "fetch", fake):
            data = fetch_market.us_index(2, date(2026, 11, 21))
        self.assertEqual(data.row, [["2026-11-19", 7175.83, 100.12, 100.12, 100.12, 100.12, 100.12]])
        self.assertIn("FRED｜SP500", [itor["名稱"] for itor in data.source])
        self.assertTrue(any(itor.startswith("S&P 500（Yahoo Finance）") for itor in data.missing))

    def test_market_section_lists_us_index(self):
        import collect
        us = source.Dataset("us_index", "美股", ["日期", "S&P 500", "VIX"])
        us.row = [["2026-11-19", 7175.83, 28.44], ["2026-11-20", 6910.39, 35.89]]
        text = collect.market_section({"taiex": source.Dataset("taiex", "", []), "us_index": us})
        self.assertIn("- S&P 500 6,910（2026-11-20）；報酬 1 日 -3.7%", text)
        self.assertIn("- VIX 35.89（2026-11-20）；前一交易日 28.44，近 1 年 28.44～35.89", text)

    def test_parse_balance_and_cashflow(self):
        balance = fetch_data.parse_report((DATA / "t164sb03_8936_115q3.html").read_text(encoding="utf-8"))
        flow = fetch_data.parse_report((DATA / "t164sb05_8936_115q3.html").read_text(encoding="utf-8"))
        self.assertEqual(balance["合約資產－流動"], 3377053)
        self.assertEqual(balance["歸屬於母公司業主之權益合計"], 6689966)
        self.assertEqual(fetch_data.pick(balance, fetch_data.BALANCE["有息負債"]), 905000 + 50000 + 1419176 + 1975954)
        self.assertEqual(flow["營業活動之淨現金流入（流出）"], -217057)
        self.assertEqual(flow["取得不動產、廠房及設備"], -404746)

    def test_position_skips_unpublished_quarter(self):
        page = {"t164sb03": (DATA / "t164sb03_8936_115q3.html").read_text(encoding="utf-8"),
                "t164sb05": (DATA / "t164sb05_8936_115q3.html").read_text(encoding="utf-8")}

        def report(name, ticker, year, season):
            return fetch_data.parse_report(page[name]) if (year, season) == (2026, 3) else {}

        with mock.patch.object(fetch_data, "report", report):
            data = fetch_data.position("8936", 1, date(2027, 1, 5))
        self.assertEqual([r[0] for r in data.row], ["2026Q3"])
        self.assertIn("2026Q4 財報尚未公告", data.missing)
        self.assertEqual(data.row[0][data.column.index("營業現金流")], -217057)

    def test_total_return_adds_back_dividend(self):
        import collect
        price = source.Dataset("price", "", ["日期", "開盤", "最高", "最低", "收盤", "成交量"])
        price.row = [["2026-10-21", 0, 0, 0, 51.2, 0], ["2026-10-22", 0, 0, 0, 47.85, 0], ["2026-10-23", 0, 0, 0, 47.85, 0]]
        dividend = source.Dataset("dividend", "", ["日期", "權值+息值", "類別"])
        dividend.row = [["2026-10-22", 3.0, "除息"]]
        level, paid = collect.total_return({"price": price, "dividend": dividend})
        self.assertTrue(paid)
        self.assertAlmostEqual(level[-1], 50.85 / 51.2)

    def test_position_section_ratios(self):
        import collect
        column = ["季"] + list(fetch_data.BALANCE) + list(fetch_data.CASHFLOW)
        position = source.Dataset("position", "", column)
        base = dict(現金=100, 應收票據及帳款=200, 合約資產=400, 存貨=0, 合約負債=0, 有息負債=300, 負債總額=0,
                    歸母權益=1000, 普通股股本=100, 折舊=0)
        position.row = [[q] + [dict(base, 營業現金流=cash, 資本支出=capex).get(k) for k in column[1:]]
                        for q, cash, capex in (("2026Q1", 50, -10), ("2026Q2", 30, -20))]
        financial = source.Dataset("financial", "", [])
        financial.row = [[q, 300, 0, 0, 0, 10, 0, 0, 0] for q in ("2025Q3", "2025Q4", "2026Q1", "2026Q2")]
        text = collect.position_section({"position": position, "financial": financial})
        self.assertIn("| 2026Q2 | 0.00 | 0.00 | 0.00 | 0.00 | 50.0 | 0.00 | 100.00 |", text)
        self.assertIn("| 2026Q2 | -0.00 | -0.00 | -0.00 |", text)


if __name__ == "__main__":
    unittest.main()
