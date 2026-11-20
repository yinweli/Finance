"""結算到期的舊報告：比對研究日 12 個月後的實際收盤落在哪個情境，累積校準統計。

舊報告另有 6 個月預測，仍照常結算，統計時和 12 個月分開。

結算用目標日（含）之後第一個收盤價，不用執行當天的價格，所以多久執行一次都不影響結果。
情境價格是除息後市價，實際收盤也直接比較，不另加配息。

用法：python -X utf8 -B 工具/settle.py            # 結算到期報告並列出校準統計
"""
import argparse
import json
import sys
from datetime import date, timedelta

import fetch_data
import render
from check import CASE
from source import today as now

RECORD = render.REPORT / "結算紀錄.json"
MONTH = {"6個月": 6, "12個月": 12}
# 同一檔在這個天數內的多份報告高度相關，校準統計只取最新一份
DEDUPE_DAYS = 30


def months_later(day, count):
    month = day.month - 1 + count
    year, month = day.year + month // 12, month % 12 + 1
    for itor in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, itor))
        except ValueError:
            continue
    raise ValueError(day)


def band(case, price):
    """以相鄰情境價格的中點分界，判斷價格落在哪個情境。"""
    if price < (case["保守"]["價格"] + case["中性"]["價格"]) / 2:
        return "保守"
    if price > (case["中性"]["價格"] + case["樂觀"]["價格"]) / 2:
        return "樂觀"
    return "中性"


def brier(case, hit):
    return sum((case[n]["機率"] / 100 - (1 if n == hit else 0)) ** 2 for n in CASE)


def close_after(ticker, target, today):
    """目標日（含）之後第一個收盤，回傳 (日期, 價格)；還沒有收盤時回傳 None。"""
    end = min(today, target + timedelta(days=20))
    exchange = fetch_data.detect(ticker, end)
    row = fetch_data.daily(ticker, exchange, 2, end).row
    later = [itor for itor in row if itor[0] >= target.isoformat() and itor[0] < today.isoformat()]
    return (later[0][0], later[0][4]) if later else None


def load():
    return json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else {}


def settle(today=None, log=print, fetch=close_after):
    """結算所有到期但尚未結算的報告，回傳新結算的紀錄。單檔抓價失敗時跳過，下次再試。"""
    today = today or now()
    record, added = load(), []
    for path in sorted(render.REPORT.glob("*/*.html")):
        data = render.extract(path)
        if not data:
            continue
        key = data["關鍵數字"]
        for horizon in [h for h in MONTH if h in key["情境"]]:
            name = f"{path.parent.name}/{path.name}|{horizon}"
            target = months_later(date.fromisoformat(key["研究日"]), MONTH[horizon])
            if name in record or target >= today:
                continue
            try:
                found = fetch(key["代號"], target, today)
            except Exception as error:  # 網路或資料源問題不影響研究流程
                log(f"結算 {name} 失敗：{error}")
                continue
            if not found:
                continue
            case = key["情境"][horizon]
            hit = band(case, found[1])
            record[name] = {"代號": key["代號"], "研究日": key["研究日"], "期間": horizon, "目標日": target.isoformat(),
                            "實際日": found[0], "實際價": found[1], "研究時現價": key["現價"]["價格"], "落點": hit,
                            "情境": {n: {"價格": case[n]["價格"], "機率": case[n]["機率"]} for n in CASE},
                            "Brier": round(brier(case, hit), 4)}
            added.append(record[name])
            log(f"結算 {name}：實際 {found[1]} 落在{hit}情境（當時機率 {case[hit]['機率']}%）")
    if added:
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def dedupe(row):
    """同一檔同一期間，研究日相距 DEDUPE_DAYS 天內的紀錄只保留最新一份。"""
    keep = []
    for itor in sorted(row, key=lambda r: r["研究日"], reverse=True):
        day = date.fromisoformat(itor["研究日"])
        if all(k["代號"] != itor["代號"] or k["期間"] != itor["期間"]
               or (date.fromisoformat(k["研究日"]) - day).days > DEDUPE_DAYS for k in keep):
            keep.append(itor)
    return keep


def stats(record):
    """各期間的樣本數、平均 Brier 分數，以及各情境的平均機率和實際命中率。"""
    result = {}
    row = dedupe(list(record.values()))
    for horizon in MONTH:
        part = [r for r in row if r["期間"] == horizon]
        if not part:
            continue
        result[horizon] = {"樣本": len(part), "平均Brier": sum(r["Brier"] for r in part) / len(part),
                           "情境": {n: {"平均機率": sum(r["情境"][n]["機率"] for r in part) / len(part),
                                       "命中率": sum(r["落點"] == n for r in part) / len(part) * 100} for n in CASE}}
    return result


def history(ticker, record=None, limit=5):
    """某檔最近幾筆結算結果，寫進上次摘要給研究者參考。"""
    record = load() if record is None else record
    row = sorted((r for r in record.values() if r["代號"] == ticker), key=lambda r: (r["研究日"], r["期間"]), reverse=True)
    return [f"{r['研究日']} 的 {r['期間']}預測：{r['實際日']} 收盤 {r['實際價']}（研究時 {r['研究時現價']}），"
            f"落在{r['落點']}情境，當時給的機率為 {r['情境'][r['落點']]['機率']}%" for r in row[:limit]]


def cli():
    parser = argparse.ArgumentParser(description="結算到期報告並列出校準統計")
    parser.add_argument("--date", type=date.fromisoformat, default=now())
    arg = parser.parse_args()
    settle(arg.date)
    result = stats(load())
    if not result:
        print("目前沒有到期可結算的報告。")
        return 0
    for horizon, value in result.items():
        print(f"## {horizon}：{value['樣本']} 筆，平均 Brier {value['平均Brier']:.3f}（越低越好，三情境平均猜約 0.667）")
        for name, itor in value["情境"].items():
            print(f"- {name}：平均給 {itor['平均機率']:.0f}%，實際命中 {itor['命中率']:.0f}%")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(cli())
