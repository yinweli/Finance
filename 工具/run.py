"""全自動研究：抓取 → 補充 → 草稿 → 審查 → 發布。流程規則見 流程/研究流程.md。

用法：python -X utf8 -B 工具/run.py 0050 8936 [--date YYYY-MM-DD] [--restart]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import agents
import check
import collect
import readme
import render
import settle
from source import ROOT, today as now

WORK = ROOT / ".tmp" / "工作"
STEPS = ("抓取", "補充", "草稿", "審查", "發布")
METHOD = {"個股": "方法/個股.md", "股票ETF": "方法/股票ETF.md", "債券ETF": "方法/債券ETF.md"}
# 補充與補查以搜尋整理為主，用較省額度的模型；審查維持 Claude Code 的預設模型
SEARCH_MODEL = "sonnet"
GROUND = "精確數字一律以資料摘要與 資料/*.json 為準；不同來源的數字衝突時寫出差異，不要自行調和出一個數字。"


class Paused(Exception):
    """額度不足，整批暫停。"""


class Manual(Exception):
    """自動重試後仍失敗，這一檔交給人工處理。"""


def rel(path):
    """專案內的路徑改成相對路徑，方便閱讀。"""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


class Job:
    def __init__(self, ticker, day, restart=False):
        self.ticker, self.day = ticker, day
        self.work = WORK / f"{day.isoformat()}-{ticker}"
        if restart and self.work.exists():
            backup = self.work.with_name(self.work.name + time.strftime("-舊%H%M%S"))
            self.work.rename(backup)
            log(f"{ticker}：舊工作夾改名為 {backup.name}")
        self.state_path = self.work / "狀態.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {
            "代號": ticker, "研究日": day.isoformat(), "步驟": {}, "codex": None, "結果": "進行中"}
        if self.state.get("結果") == "已發布":
            # 同一天已發布過，另開新版本工作夾
            version = 2
            while (WORK / f"{day.isoformat()}-{ticker}-v{version}").exists():
                version += 1
            self.work = WORK / f"{day.isoformat()}-{ticker}-v{version}"
            self.state_path = self.work / "狀態.json"
            self.state = {"代號": ticker, "研究日": day.isoformat(), "步驟": {}, "codex": None, "結果": "進行中"}
        self.work.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        return self.work / name

    def save(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def done(self, step):
        return self.state["步驟"].get(step, {}).get("狀態") == "完成"

    def mark(self, step, status, note=None):
        entry = {"狀態": status, "時間": time.strftime("%Y-%m-%d %H:%M:%S")}
        if note:
            entry["說明"] = note
        self.state["步驟"][step] = entry
        self.save()

    @property
    def profile(self):
        return json.loads(self.path("資料/標的.json").read_text(encoding="utf-8"))

    def doc(self):
        """給 AI 的共用路徑說明。"""
        info = self.profile
        target = f"- 標的文件：{ROOT / '標的' / (self.ticker + '.md')}\n" if info.get("標的文件") else "- 標的文件：無，依研究方法通則研究\n"
        return (f"- 標的：{info['代號']} {info['名稱']}（{info['類型']}，{info['交易所']}）\n"
                f"- 研究日：{self.day.isoformat()}；最新收盤 {info.get('收盤')} 元（{info.get('收盤日')}）\n"
                f"- 工作夾（目前目錄）：{self.work}\n"
                f"- 專案根目錄：{ROOT}\n"
                f"- 研究方法：{ROOT / METHOD[info['類型']]}\n" + target)


# 各步驟

def step_fetch(job):
    log(f"{job.ticker}：抓取資料")
    collect.collect(job.ticker, job.work, job.day, log=lambda m: log(f"{job.ticker}：  {m}"))
    report, data = render.latest(job.ticker)
    if data:
        key = data["關鍵數字"]
        lines = [f"# 上次研究摘要", "", f"- 報告：{rel(report)}", f"- 研究日：{key['研究日']}",
                 f"- 現價：{key['現價']['價格']}（{key['現價']['日期']}）",
                 f"- 合理區間：{key['合理區間']['低']}～{key['合理區間']['高']}（{key['合理區間']['方法']}）",
                 f"- 結論：{key['結論']}", "", "## 情境"]
        for horizon in [h for h in settle.MONTH if h in key["情境"]]:
            lines.append(f"- {horizon}：" + "、".join(
                f"{n} {key['情境'][horizon][n]['價格']}（{key['情境'][horizon][n]['機率']}%）" for n in check.CASE))
        lines += ["", "## 重估條件"] + [f"- {r['指標']}：{r['門檻']} → {r['動作']}" for r in key["重估條件"]]
        lines += ["", "## 事件"] + [f"- {e['日期']} {e['名稱']}（{e['狀態']}，{e['機率']}%，影響 {e['影響']}%）" for e in key["事件"]]
        if key.get("檢查點"):
            lines += ["", "## 檢查點（請逐項判斷已達成、落後或尚未到期）"] + [
                f"- {p['日期']} {p['指標']}：預期 {p['預期']}；落後時 {p['落後時']}" for p in key["檢查點"]]
        lines += ["", "## 期中進度"] + progress(key, job.profile)
        past = settle.history(job.ticker)
        if past:
            lines += ["", "## 過去預測的結算結果"] + [f"- {itor}" for itor in past]
        job.path("上次摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def progress(key, info):
    """上次研究至今的股價變化，以及目前價格落在上次 12 個月情境的哪一段。"""
    price, last = info.get("收盤"), key["現價"]["價格"]
    if not price:
        return ["- 無最新收盤，無法比較"]
    case = key["情境"]["12個月"]
    return [f"- 上次研究至今股價 {(price / last - 1) * 100:+.1f}%（{last} → {price}，{info.get('收盤日')}）",
            f"- 目前價格落在上次 12 個月情境的{settle.band(case, price)}區段"
            f"（保守 {case['保守']['價格']}、中性 {case['中性']['價格']}、樂觀 {case['樂觀']['價格']}）"]


def step_supplement(job):
    log(f"{job.ticker}：Claude 補充資料")
    prompt = (f"你是 Finance 研究流程的「資料補充者」。依 {ROOT / '流程' / '補充資料.md'} 的規則，"
              f"替下列標的補充腳本抓不到的資料，寫入工作夾的 `補充資料.md`。\n\n{job.doc()}"
              f"- 腳本資料摘要：{job.path('資料摘要.md')}\n\n完成後只回覆 done。")
    agents.claude(prompt, job.work, "補充", model=SEARCH_MODEL)
    if not job.path("補充資料.md").exists():
        raise agents.AgentError("Claude 沒有產生 補充資料.md")


def codex_draft(job, prompt, step):
    reply, thread = agents.codex(prompt, job.work, step, job.state.get("codex"))
    job.state["codex"] = thread
    job.save()
    return reply


def draft_problem(job, name="草稿.md"):
    problem, key = check.check_file(job.path(name), job.work)
    return problem, key


def step_draft(job):
    log(f"{job.ticker}：Codex 撰寫草稿")
    info = job.profile
    extra = f"- 上次研究摘要：{job.path('上次摘要.md')}（請在「結論」最後說明和上次比較的變化）\n" if job.path("上次摘要.md").exists() else ""
    prompt = (f"你是 Finance 研究流程的「研究者」。請依下列文件寫出研究草稿，存成工作夾的 `草稿.md`。\n\n{job.doc()}{extra}"
              f"- 草稿格式（必須完全遵守）：{ROOT / '流程' / '草稿格式.md'}\n"
              f"- 腳本資料摘要：{job.path('資料摘要.md')}；細節可讀 {job.path('資料')} 內的 JSON\n"
              f"- 補充資料（Claude 查證的法說、新聞、規則）：{job.path('補充資料.md')}\n\n"
              "規則：\n"
              "1. 只能使用上列文件與工作夾內的資料，不要上網，也不要載入任何 skill。資料不足時寫進關鍵數字的「資料缺口」。"
              + GROUND + "\n"
              f"2. 關鍵數字的「現價」必須是 {info.get('收盤')}，日期 {info.get('收盤日')}。\n"
              "3. 寫完後執行 `python -X utf8 -B " + str(ROOT / "工具" / "check.py") + " 草稿.md` 自我檢查，修到通過為止。\n"
              "4. 完成後只回覆 done。")
    codex_draft(job, prompt, "草稿")
    problem, key = draft_problem(job)
    if problem:
        log(f"{job.ticker}：草稿未通過檢查，退回 Codex 修正一次")
        codex_draft(job, "草稿.md 未通過格式檢查，請修正以下問題後覆寫 草稿.md，完成後只回覆 done：\n"
                    + "\n".join(f"- {p}" for p in problem), "草稿修正")
        problem, key = draft_problem(job)
        if problem:
            raise Manual("草稿修正後仍未通過檢查：" + "；".join(problem[:5]))
    # 只有查得到、而且會影響結論的缺口才值得再查一次；其餘只寫進報告的限制
    gap = [g for g in key.get("資料缺口") or [] if g.get("可查") and g.get("影響結論")]
    if gap and not job.state.get("已補查"):
        log(f"{job.ticker}：草稿列出 {len(gap)} 項可查且影響結論的資料缺口，Claude 補查一次")
        prompt = (f"你是 Finance 研究流程的「資料補充者」。研究者在草稿中列出下列資料缺口，請依 {ROOT / '流程' / '補充資料.md'} "
                  "的規則查證，把結果加在工作夾 `補充資料.md` 最後的「## 補查」章節。查不到的項目寫明查過哪些來源。\n\n"
                  + job.doc() + "\n資料缺口：\n" + "\n".join(f"- {g.get('項目')}：{g.get('影響')}" for g in gap)
                  + "\n\n完成後只回覆 done。")
        agents.claude(prompt, job.work, "補查", model=SEARCH_MODEL)
        job.state["已補查"] = True
        job.save()
        codex_draft(job, "Claude 已依資料缺口補查，結果在 補充資料.md 的「## 補查」章節。請據以更新 草稿.md："
                    "已補到的資料併入分析並從「資料缺口」移除，仍查不到的保留。更新後執行 check.py 自我檢查，完成後只回覆 done。", "草稿補查")
        problem, key = draft_problem(job)
        if problem:
            raise Manual("補查後的草稿未通過檢查：" + "；".join(problem[:5]))


BLOCK = re.compile(r"[［\[]阻擋[］\]]")


def review_verdict(job):
    text = job.path("審查.md").read_text(encoding="utf-8") if job.path("審查.md").exists() else ""
    found = re.search(r"結論[:：]\s*(通過|退回)", text)
    return found.group(1) if found else None


def step_review(job):
    for round_ in (1, 2):
        log(f"{job.ticker}：Claude 審查（第 {round_} 輪）")
        prompt = (f"你是 Finance 研究流程的「審查者」。依 {ROOT / '流程' / '審查清單.md'} 審查工作夾的 `草稿.md`，"
                  f"只寫出 `審查.md`，不要產出定稿：研究者會依你的意見修正。\n\n{job.doc()}"
                  f"- 草稿格式：{ROOT / '流程' / '草稿格式.md'}\n"
                  f"- 這是第 {round_} 輪審查。" + ("第 2 輪不可再退回。\n" if round_ == 2 else "\n")
                  + GROUND + "\n\n完成後只回覆「通過」或「退回」。")
        agents.claude(prompt, job.work, f"審查{round_}")
        verdict = review_verdict(job)
        if verdict is None:
            raise Manual("審查.md 沒有寫出「結論：通過」或「結論：退回」")
        if verdict == "退回":
            if round_ == 2:
                raise Manual("第 2 輪審查仍退回，需要人工判斷")
            log(f"{job.ticker}：審查退回，Codex 依審查意見修正")
            codex_draft(job, "審查者退回草稿，意見在工作夾的 審查.md。請逐項修正 草稿.md，並在 審查.md 每則阻擋意見下補一行"
                        "「回覆：已修正」或不修正的理由。修正後執行 check.py 自我檢查，完成後只回覆 done。", "草稿退修")
            problem, _ = draft_problem(job)
            if problem:
                raise Manual("退修後的草稿未通過檢查：" + "；".join(problem[:5]))
            continue
        break
    log(f"{job.ticker}：Codex 依審查意見產出定稿")
    codex_draft(job, "審查者的意見在工作夾的 審查.md。請以 草稿.md 為底稿，處理所有「阻擋」與「應修」意見，「建議」可斟酌採用，"
                "另存為 定稿.md，草稿.md 保持不變。在 審查.md 每則阻擋與應修意見下補一行「回覆：已修正」或不修正的理由。"
                "存檔後執行 `python -X utf8 -B " + str(ROOT / "工具" / "check.py") + " 定稿.md` 自我檢查，"
                "修到通過為止，完成後只回覆 done。", "定稿")
    problem, _ = draft_problem(job, "定稿.md")
    if problem:
        log(f"{job.ticker}：定稿未通過檢查，退回 Codex 修正一次")
        codex_draft(job, "定稿.md 未通過格式檢查，請只修正下列問題後覆寫 定稿.md，完成後只回覆 done：\n"
                    + "\n".join(f"- {p}" for p in problem), "定稿修正")
        problem, _ = draft_problem(job, "定稿.md")
        if problem:
            raise Manual("定稿修正後仍未通過檢查：" + "；".join(problem[:5]))
    if BLOCK.search(job.path("審查.md").read_text(encoding="utf-8")):
        # 只有阻擋級意見需要審查者確認，其餘交給研究者處理，省下一輪完整審查
        log(f"{job.ticker}：Claude 複核阻擋意見")
        agents.claude("你是 Finance 研究流程的「審查者」。你在 審查.md 提出的阻擋意見，研究者已修正並存為 定稿.md。"
                      "請只核對每則阻擋意見是否確實修正，只讀 審查.md、定稿.md 與核對需要的資料，不要重新審查全文。"
                      "未修正的直接修改 定稿.md，並在 審查.md 最後加上「## 複核」章節逐則記錄結果。完成後只回覆 done。",
                      job.work, "複核", tools=("Read", "Glob", "Grep", "Write", "Edit"))
        problem, _ = draft_problem(job, "定稿.md")
        if problem:
            raise Manual("複核後的定稿未通過檢查：" + "；".join(problem[:5]))


def run_setting():
    """這次研究使用的程式版本與模型，寫進報告，日後才能依方法版本比較校準結果。"""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    option = agents.codex_config()
    return {"程式版本": commit or "未知", "Claude 補充模型": SEARCH_MODEL, "Claude 審查模型": "Claude Code 預設",
            "Codex 模型": option[option.index("-m") + 1] if "-m" in option else "Codex 預設"}


def step_publish(job):
    job.path("執行設定.json").write_text(json.dumps(run_setting(), ensure_ascii=False, indent=1), encoding="utf-8")
    target = render.render(job.path("定稿.md"), job.work)
    job.state["報告"] = rel(target)
    log(f"{job.ticker}：已發布 {job.state['報告']}")
    if not readme.update():
        log("README.md 缺少最新報告標記，未更新連結")


ACTION = {"抓取": step_fetch, "補充": step_supplement, "草稿": step_draft, "審查": step_review, "發布": step_publish}


def research(job):
    for step in STEPS:
        if job.done(step):
            continue
        job.mark(step, "進行中")
        try:
            ACTION[step](job)
        except agents.AgentError as error:
            if error.kind == "quota":
                job.mark(step, "暫停", str(error))
                job.state["結果"] = "暫停"
                job.save()
                raise Paused(f"{job.ticker} 在「{step}」額度不足：{error}") from None
            # 一般錯誤自動重試一次
            log(f"{job.ticker}：「{step}」失敗，重試一次：{error}")
            try:
                ACTION[step](job)
            except agents.AgentError as again:
                if again.kind == "quota":
                    job.mark(step, "暫停", str(again))
                    job.state["結果"] = "暫停"
                    job.save()
                    raise Paused(f"{job.ticker} 在「{step}」額度不足：{again}") from None
                raise Manual(f"「{step}」重試後仍失敗：{again}") from None
        except (Manual, ValueError, OSError) as error:
            raise Manual(f"「{step}」：{error}") from None
        job.mark(step, "完成")
    job.state["結果"] = "已發布"
    job.save()


def summary_line(job):
    state = job.state
    line = f"- {job.ticker}：{state['結果']}"
    if state.get("報告"):
        report = ROOT / state["報告"]
        data = render.extract(report) or {}
        key, result = data.get("關鍵數字", {}), data.get("計算", {})
        if key:
            line += (f"｜現價 {key['現價']['價格']}｜合理區間 {key['合理區間']['低']}～{key['合理區間']['高']}"
                     f"｜12個月期望 {result['12個月']['期望']['價格']:.2f}"
                     f"\n  - 結論：{key['結論']}\n  - 報告：{state['報告']}")
    else:
        failed = [f"{k}：{v.get('說明', v['狀態'])}" for k, v in state["步驟"].items() if v["狀態"] != "完成"]
        if failed:
            line += f"\n  - {'；'.join(failed)}\n  - 工作夾：{rel(job.work)}"
    return line


def main():
    parser = argparse.ArgumentParser(description="全自動研究台股標的")
    parser.add_argument("ticker", nargs="+")
    parser.add_argument("--date", type=date.fromisoformat, default=now())
    parser.add_argument("--restart", action="store_true", help="捨棄今天未完成的工作夾，從頭開始")
    arg = parser.parse_args()
    for name in ("claude", "codex"):
        if not shutil.which(name):
            sys.exit(f"找不到 {name} 指令")
    try:
        settle.settle(arg.date, log=log)
    except Exception as error:  # 結算只是附加資訊，失敗不影響研究
        log(f"結算舊報告失敗：{error}")
    jobs, code = [], 0
    for ticker in arg.ticker:
        job = Job(ticker, arg.date, arg.restart)
        jobs.append(job)
        try:
            research(job)
        except Manual as error:
            job.state["結果"] = "待人工"
            job.save()
            log(f"{ticker}：待人工處理，{error}")
            for step, value in job.state["步驟"].items():
                if value["狀態"] == "進行中":
                    job.mark(step, "失敗", str(error))
            code = 1
        except Paused as error:
            log(f"整批暫停：{error}")
            code = 2
            break
    print("\n## 研究結果\n")
    print("\n".join(summary_line(job) for job in jobs))
    rest = arg.ticker[len(jobs):]
    if code == 2:
        print(f"\n額度恢復後執行相同指令即可從中斷處繼續。尚未開始：{'、'.join(rest) or '無'}")
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
