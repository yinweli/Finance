import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "工具"))

import agents  # noqa: E402
import check  # noqa: E402
import opinion  # noqa: E402
import readme  # noqa: E402
import render  # noqa: E402
import run  # noqa: E402
import settle  # noqa: E402

KEY = {
    "代號": "8936", "名稱": "國統", "類型": "個股", "研究日": "2026-11-19",
    "現價": {"價格": 40.0, "日期": "2026-11-18"},
    "合理區間": {"低": 38.0, "高": 50.0, "方法": "EPS 3.2 × 12～15.6 倍"},
    "情境": {h: {"保守": {"價格": 35.0, "機率": 25, "假設": "驗收延後"},
                 "中性": {"價格": 44.0, "機率": 50, "假設": "依進度認列"},
                 "樂觀": {"價格": 52.0, "機率": 25, "假設": "新案提前"}} for h in ("12個月",)},
    "配息": {"12個月": 2.0},
    "評等": {"籌碼": "偏多", "市場": "中性"},
    "季預估": [{"季": f"{2026 + (q + 3) // 4}Q{(q + 3) % 4 + 1}", "營收": 17.0, "毛利率": 20.0,
               "EPS": {"保守": 0.6, "中性": 0.8, "樂觀": 0.9}} for q in range(8)],
    "事件": [{"名稱": "工程驗收", "日期": "2027-03", "狀態": "預期", "機率": 60, "影響": 5.0, "情境": "樂觀",
              "說明": "認列剩餘 8 億元", "來源": "補充資料 [1]"}],
    "檢查點": [{"日期": "2027-03", "指標": "2026Q4 EPS", "預期": "中性 0.8 元", "落後時": "下修中性情境"},
              {"日期": "2027-08", "指標": "2027Q2 營收", "預期": "17 億元", "落後時": "重新估值"}],
    "重估條件": [{"指標": f"指標{i}", "門檻": "門檻", "動作": "動作"} for i in range(3)],
    "資料缺口": [],
    "結論": "現價在合理區間下緣，可分批買進。",
}


def draft(key=None, section=None):
    key = key or KEY
    section = section or check.SECTIONS[key["類型"]][1:]
    body = [f"# {key['代號']} {key['名稱']} 研究", "", "## 關鍵數字", "", "```json",
            json.dumps(key, ensure_ascii=False, indent=1), "```"]
    for name in section:
        body += ["", f"## {name}", "", f"{name}的內容。", "", "- 重點一", "- 重點二", "", "| 欄 | 值 |", "| --- | --- |",
                 "| a | **1** |"]
    return "\n".join(body) + "\n"


def profile(work):
    folder = Path(work) / "資料"
    folder.mkdir(parents=True, exist_ok=True)
    info = {"代號": "8936", "名稱": "國統", "類型": "個股", "交易所": "TPEx", "研究日": "2026-11-19",
            "收盤": 40.0, "收盤日": "2026-11-18", "標的文件": "標的/8936.md"}
    (folder / "標的.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    price = {"名稱": "8936 日成交", "欄位": [], "資料": [[f"2026-{m:02d}-{d:02d}", 1, 1, 1, 38 + d / 10, 100]
                                                for m in range(1, 12) for d in range(1, 21)],
             "來源": [{"名稱": "櫃買中心｜個股日成交資訊", "網址": "https://www.tpex.org.tw/"}], "缺漏": []}
    (folder / "price.json").write_text(json.dumps(price, ensure_ascii=False), encoding="utf-8")
    fin = {"名稱": "單季損益", "欄位": [], "資料": [["2026Q3", 1731811, 1, 1, 1, 192413, 0.77, 20.0, 10.0]], "來源": [], "缺漏": []}
    (folder / "financial.json").write_text(json.dumps(fin, ensure_ascii=False), encoding="utf-8")
    return info


class CheckTest(unittest.TestCase):
    def test_valid_draft_passes_and_computes(self):
        problem, key = check.check_text(draft(), {"代號": "8936", "類型": "個股", "收盤": 40.0, "收盤日": "2026-11-18"})
        self.assertEqual(problem, [])
        result = check.compute(key)
        self.assertAlmostEqual(result["12個月"]["期望"]["價格"], 43.75)
        self.assertAlmostEqual(result["12個月"]["中性"]["含息報酬"], 15.0)
        self.assertAlmostEqual(result["合理區間位置"], 100 * 2 / 12)

    def test_detects_order_probability_price_and_sections(self):
        key = copy.deepcopy(KEY)
        key["情境"]["12個月"]["保守"]["價格"] = 50.0
        key["情境"]["12個月"]["中性"]["機率"] = 40
        key["事件"][0]["狀態"] = "已發生"
        problem, _ = check.check_text(draft(key, ["結論", "籌碼"]), {"代號": "8936", "類型": "個股", "收盤": 41.0,
                                                                   "收盤日": "2026-11-18"})
        text = "\n".join(problem)
        for word in ("保守 ≤ 中性 ≤ 樂觀", "機率合計必須為 100", "現價 40.0 與資料最新收盤 41.0 不符", "機率必須為 100", "章節必須依序"):
            self.assertIn(word, text)

    def test_checkpoint_rules(self):
        key = copy.deepcopy(KEY)
        key["檢查點"] = key["檢查點"][:1]
        self.assertTrue(any("檢查點至少" in p for p in check.check_text(draft(key))[0]))
        key = copy.deepcopy(KEY)
        key["檢查點"][0]["日期"] = "2028-01"
        key["檢查點"][1]["日期"] = "2027/08"
        text = " ".join(check.check_text(draft(key))[0])
        self.assertIn("12 個月內", text)
        self.assertIn("YYYY-MM", text)
        key = copy.deepcopy(KEY)
        key["檢查點"][0]["日期"] = "2027-07"
        self.assertTrue(any("6 個月內" in p for p in check.check_text(draft(key))[0]))

    def test_gap_fields_required(self):
        key = copy.deepcopy(KEY)
        key["資料缺口"] = [{"項目": "法說", "影響": "EPS"}]
        problem, _ = check.check_text(draft(key))
        self.assertTrue(any("可查" in p for p in problem))

    def test_formula_must_match_fair_range(self):
        key = copy.deepcopy(KEY)
        key["合理區間"]["方法"] = "未來 4 季 EPS 3.20 元 × 本益比 12.5～16.3 倍"
        key["合理區間"].update({"低": 40.0, "高": 52.2})
        self.assertEqual(check.check_text(draft(key))[0], [])
        key["合理區間"]["高"] = 60.0
        self.assertTrue(any("方法算式" in p for p in check.check_text(draft(key))[0]))

    def test_chance_far_from_base_needs_reason(self):
        info = {"代號": "8936", "類型": "個股", "收盤": 40.0, "收盤日": "2026-11-18", "年化波動度": 5.0}
        base = check.base_chance(KEY, info)
        self.assertAlmostEqual(sum(base["12個月"].values()), 100)
        # 波動度很低時，股價幾乎不可能漲到樂觀區段，給樂觀 25% 就偏離基準太多
        problem, _ = check.check_text(draft(), info)
        self.assertTrue(any("機率說明" in p for p in problem))
        key = copy.deepcopy(KEY)
        key["機率說明"] = "工程驗收時點不確定，事件風險高於歷史波動"
        self.assertEqual(check.check_text(draft(key), info)[0], [])

    def test_missing_json_block(self):
        problem, key = check.check_text("# x\n\n## 關鍵數字\n\n沒有區塊\n")
        self.assertIsNone(key)
        self.assertIn("json", problem[0])


class RenderTest(unittest.TestCase):
    def test_render_report_and_extract(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder) / "work"
            profile(work)
            (work / "定稿.md").write_text(draft(), encoding="utf-8")
            target = render.render(work / "定稿.md", work, Path(folder) / "out.html")
            page = target.read_text(encoding="utf-8")
            for word in ("合理區間與 12 個月情境", "股價與 12 個月情境", "期中檢查點", "2026Q4 EPS", "單季 EPS", "<table", "<li>重點一</li>",
                         "<strong>1</strong>", "工程驗收", "櫃買中心｜個股日成交資訊"):
                self.assertIn(word, page)
            self.assertNotIn("{{", page)
            data = render.extract(target)
            self.assertEqual(data["關鍵數字"]["代號"], "8936")

    def test_output_path_versions_and_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "8936"
            base.mkdir()
            self.assertEqual(render.output_path(KEY, folder).name, "2026-11-19.html")
            (base / "2026-11-19.html").write_text("x", encoding="utf-8")
            self.assertEqual(render.output_path(KEY, folder).name, "2026-11-19-v2.html")
            (base / "2026-11-19-v2.html").write_text("x", encoding="utf-8")
            (base / "2026-10-01.html").write_text("x", encoding="utf-8")
            self.assertEqual(render.latest("8936", folder)[0].name, "2026-11-19-v2.html")

    def test_markdown_escapes_html(self):
        self.assertIn("&lt;script&gt;", render.markdown("<script>alert(1)</script>"))


class ReadmeTest(unittest.TestCase):
    def test_lists_latest_three_reports_per_ticker(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "報告"
            base = report / "8936"
            base.mkdir(parents=True)
            data = json.dumps({"關鍵數字": {"名稱": "國統"}}, ensure_ascii=False)
            for name in ("2026-10-01", "2026-11-19", "2026-11-19-v2", "2026-11-20"):
                (base / f"{name}.html").write_text(
                    f'<script type="application/json" id="research-data">{data}</script>', encoding="utf-8")
            path = Path(folder) / "README.md"
            path.write_text(f"# 前\n\n{readme.BEGIN}\n舊內容\n{readme.END}\n\n# 後\n", encoding="utf-8")
            self.assertTrue(readme.update(path, report))
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"| 8936 國統 | [2026-11-20]({readme.PAGES}8936/2026-11-20.html)、[2026-11-19-v2]", text)
            self.assertNotIn("2026-10-01", text)
            self.assertNotIn("舊內容", text)
            self.assertTrue(text.endswith("# 後\n"))

    def test_skips_opinion_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / render.OPINION
            base.mkdir()
            (base / "2026-11-20-下週.html").write_text("<p>意見</p>", encoding="utf-8")
            self.assertNotIn(render.OPINION, readme.table(folder))

    def test_missing_marker_leaves_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "README.md"
            path.write_text("# 無標記\n", encoding="utf-8")
            self.assertFalse(readme.update(path, folder))
            self.assertEqual(path.read_text(encoding="utf-8"), "# 無標記\n")


class OpinionTest(unittest.TestCase):
    TEXT = ("# 下週操作方向\n\n- 撰寫日：2026-11-20\n\n## 結論\n\n先等。\n\n"
            "## 持股\n\n| 代號 | 看法 |\n|---|---|\n| 0050 | 續抱 |\n")

    def test_build_sections_nav_and_footer(self):
        page = opinion.build(self.TEXT)
        self.assertIn("<title>下週操作方向</title>", page)
        self.assertIn('<a href="#sec-0">結論</a><a href="#sec-1">持股</a>', page)
        self.assertIn("<td>0050</td>", page)
        self.assertIn("撰寫日：2026-11-20", page)
        self.assertIn(opinion.FOOTER, page)
        self.assertNotIn("{{", page)

    def test_publish_uses_markdown_name(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "2026-11-20-下週.md"
            source.write_text(self.TEXT, encoding="utf-8")
            target = opinion.publish(source, Path(folder) / "out" / "x.html")
            self.assertTrue(target.exists())
            self.assertIsNone(render.extract(target))


class RunTest(unittest.TestCase):
    """用假的 Claude 與 Codex 跑完整流程，確認串接、檢查與重試。"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "README.md").write_text(f"{readme.BEGIN}\n{readme.END}\n", encoding="utf-8")
        patch = [mock.patch.object(run, "WORK", self.root / "工作"), mock.patch.object(render, "REPORT", self.root / "報告"),
                 mock.patch.object(readme, "README", self.root / "README.md"),
                 mock.patch.object(run.collect, "collect", lambda t, w, d, log=None: profile(w)),
                 mock.patch.object(run, "log", lambda m: None)]
        for itor in patch:
            itor.start()
            self.addCleanup(itor.stop)
        self.call = []

    def fake_claude(self, verdict="通過", note=""):
        def claude(prompt, work, step, tools=None, timeout=0, model=None):
            self.call.append(("claude", step, model))
            work = Path(work)
            if step in ("補充", "補查"):
                (work / "補充資料.md").write_text("# 補充\n", encoding="utf-8")
            elif step.startswith("審查"):
                (work / "審查.md").write_text(f"# 審查\n\n- 結論：{verdict}\n{note}", encoding="utf-8")
            return "done", {}
        return claude

    def fake_codex(self, bad_first=False, key=None):
        def codex(prompt, work, step, thread=None, timeout=0):
            self.call.append(("codex", step))
            text = draft(key)
            if bad_first and step == "草稿":
                text = text.replace('"價格": 40.0', '"價格": 41.0', 1)
            (Path(work) / ("定稿.md" if step.startswith("定稿") else "草稿.md")).write_text(text, encoding="utf-8")
            return "done", "thread-1"
        return codex

    def steps(self):
        return [c[:2] for c in self.call]

    def test_full_run_publishes(self):
        with mock.patch.object(agents, "claude", self.fake_claude()), mock.patch.object(agents, "codex", self.fake_codex(True)):
            job = run.Job("8936", date(2026, 11, 19))
            run.research(job)
        self.assertEqual(job.state["結果"], "已發布")
        self.assertTrue((self.root / "報告" / "8936" / "2026-11-19.html").exists())
        self.assertIn(("codex", "草稿修正"), self.steps())
        self.assertIn(("codex", "定稿"), self.steps())
        self.assertIn(("claude", "補充", run.SEARCH_MODEL), self.call)
        # 沒有阻擋意見時不複核；沒有資料缺口時不補查
        self.assertNotIn(("claude", "複核"), self.steps())
        self.assertNotIn(("claude", "補查"), self.steps())
        self.assertEqual(job.state["codex"], "thread-1")
        self.assertTrue((job.work / "執行設定.json").exists())

    def test_only_searchable_gap_triggers_recheck_and_block_triggers_confirm(self):
        key = copy.deepcopy(KEY)
        key["資料缺口"] = [{"項目": "非公開共識", "影響": "估值", "可查": False, "影響結論": True},
                       {"項目": "小細節", "影響": "無", "可查": True, "影響結論": False}]
        with mock.patch.object(agents, "claude", self.fake_claude(note="### 1［阻擋］數字錯誤\n")), \
                mock.patch.object(agents, "codex", self.fake_codex(key=key)):
            job = run.Job("8936", date(2026, 11, 19))
            run.research(job)
        self.assertNotIn(("claude", "補查"), self.steps())
        self.assertIn(("claude", "複核"), self.steps())
        key["資料缺口"][0]["可查"] = True
        self.call.clear()
        with mock.patch.object(agents, "claude", self.fake_claude()), mock.patch.object(agents, "codex", self.fake_codex(key=key)):
            job = run.Job("8936", date(2026, 11, 20))
            run.research(job)
        self.assertIn(("claude", "補查"), self.steps())

    def test_quota_pauses_and_resume_continues(self):
        def quota(*arg, **kw):
            raise agents.AgentError("usage limit reached", "quota")

        with mock.patch.object(agents, "claude", quota):
            job = run.Job("8936", date(2026, 11, 19))
            with self.assertRaises(run.Paused):
                run.research(job)
        self.assertEqual(job.state["步驟"]["補充"]["狀態"], "暫停")
        with mock.patch.object(agents, "claude", self.fake_claude()), mock.patch.object(agents, "codex", self.fake_codex()):
            job = run.Job("8936", date(2026, 11, 19))
            run.research(job)
        self.assertEqual(job.state["結果"], "已發布")

    def test_review_reject_goes_back_to_codex_once(self):
        with mock.patch.object(agents, "claude", self.fake_claude("退回")), mock.patch.object(agents, "codex", self.fake_codex()):
            job = run.Job("8936", date(2026, 11, 19))
            with self.assertRaises(run.Manual):
                run.research(job)
        # 第 2 輪仍寫退回時，審查.md 沒有「通過」也沒有定稿問題，但流程只退回一次
        self.assertEqual([c for c in self.steps() if c == ("codex", "草稿退修")], [("codex", "草稿退修")])


class SettleTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for itor in (mock.patch.object(render, "REPORT", self.root / "報告"),
                     mock.patch.object(settle, "RECORD", self.root / "報告" / "結算紀錄.json")):
            itor.start()
            self.addCleanup(itor.stop)

    def test_months_later_and_band(self):
        self.assertEqual(settle.months_later(date(2026, 8, 31), 6), date(2027, 2, 28))
        case = KEY["情境"]["12個月"]
        self.assertEqual([settle.band(case, v) for v in (30, 44, 60)], ["保守", "中性", "樂觀"])

    def test_settle_due_reports_once_and_stats(self):
        work = self.root / "工作"
        work.mkdir()
        profile(work)
        source = work / "定稿.md"
        source.write_text(draft(), encoding="utf-8")
        render.render(source, work)
        fetched = []

        def fetch(ticker, target, today):
            fetched.append(target)
            return target.isoformat(), 53.0

        self.assertEqual(settle.settle(date(2027, 6, 1), log=lambda m: None, fetch=fetch), [])
        added = settle.settle(date(2027, 12, 1), log=lambda m: None, fetch=fetch)
        self.assertEqual([r["期間"] for r in added], ["12個月"])
        self.assertEqual(added[0]["落點"], "樂觀")
        self.assertEqual(settle.settle(date(2027, 12, 1), log=lambda m: None, fetch=fetch), [])
        self.assertEqual(len(fetched), 1)
        result = settle.stats(settle.load())
        self.assertEqual(result["12個月"]["樣本"], 1)
        self.assertEqual(result["12個月"]["情境"]["樂觀"]["命中率"], 100)
        self.assertIn("落在樂觀情境", settle.history("8936")[0])

    def test_legacy_six_month_forecast_still_settles(self):
        legacy = copy.deepcopy(KEY)
        legacy["情境"]["6個月"] = copy.deepcopy(legacy["情境"]["12個月"])
        path = self.root / "報告" / "8936" / "2026-11-19.html"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        with mock.patch.object(render, "extract", return_value={"關鍵數字": legacy}):
            added = settle.settle(date(2027, 12, 1), log=lambda m: None, fetch=lambda t, d, n: (d.isoformat(), 40.0))
        self.assertEqual(sorted(r["期間"] for r in added), ["12個月", "6個月"])
        self.assertEqual(set(settle.stats(settle.load())), {"6個月", "12個月"})


if __name__ == "__main__":
    unittest.main()
