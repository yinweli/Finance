"""依標的類型抓齊研究資料，存成 資料/*.json，並產生給 AI 閱讀的 資料摘要.md。"""
import argparse
import json
import math
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import fetch_data
import fetch_market
from source import CACHE, ROOT, Dataset, today as now

TYPES = ("個股", "股票ETF", "債券ETF")
MONTHS = 26  # 行情與指數抓 26 個月，才足以計算 2 年報酬
VALUATION_MONTHS = 62  # 本益比歷史抓 5 年：業界以 3～5 年或完整景氣循環判斷合理倍數，2 年容易把近期評價當常態


def profile(ticker):
    """讀 標的/<代號>.md 的名稱與類型；沒有文件時依代號推測。"""
    path = ROOT / "標的" / f"{ticker}.md"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        title = re.search(r"^#\s+\S+\s+(.+)$", text, re.M)
        kind = re.search(r"^- 類型：(\S+)", text, re.M)
        if kind and kind.group(1) in TYPES:
            return {"代號": ticker, "名稱": title.group(1).strip() if title else ticker, "類型": kind.group(1),
                    "標的文件": f"標的/{ticker}.md"}
    kind = "債券ETF" if ticker.startswith("00") and ticker.endswith("B") else "股票ETF" if ticker.startswith("00") else "個股"
    return {"代號": ticker, "名稱": ticker, "類型": kind, "標的文件": None}


def collect(ticker, work, day, log=print):
    """抓取資料並寫入 work/資料/，回傳 {名稱: Dataset}。"""
    info = profile(ticker)
    folder = Path(work) / "資料"
    exchange = fetch_data.detect(ticker, day)
    info.update({"交易所": exchange, "研究日": day.isoformat()})
    data = {}

    def run(name, call):
        log(f"抓取 {name}")
        try:
            result = call()
        except Exception as error:  # 單項失敗不影響其他資料，記入缺漏
            log(f"  失敗：{error}")
            result = Dataset(name, name, [])
            result.missing.append(f"抓取失敗：{error}")
        for itor in result if isinstance(result, list) else [result]:
            data[itor.name] = itor

    # 先抓加權指數作為交易日曆，其餘依主機分組同時抓取：同一主機依序、不同主機並行
    run("taiex", lambda: fetch_market.taiex(MONTHS, day))
    calendar = data["taiex"]
    days60 = fetch_market.trading_days(calendar, 60)
    stock = info["類型"] == "個股"
    group = {"證交所": [("market_margin", lambda: fetch_market.market_margin(days60, day))],
             "櫃買中心": [], "觀測站": [], "其他": [("futures", lambda: fetch_market.futures(4, day))]}
    venue = group["證交所" if exchange == "TWSE" else "櫃買中心"]
    venue += [("price", lambda: fetch_data.daily(ticker, exchange, MONTHS, day)),
              ("dividend", lambda: fetch_data.dividend(ticker, exchange, day - timedelta(days=730), day))]
    if exchange == "TPEx":
        group["櫃買中心"].append(("otc_index", lambda: fetch_market.otc_index(MONTHS, day)))
    if stock:
        venue += [("valuation", lambda: fetch_data.valuation(ticker, exchange, VALUATION_MONTHS, day)),
                  ("institutional", lambda: fetch_market.institutional(ticker, exchange, days60, day)),
                  ("margin", lambda: fetch_market.margin(ticker, exchange, days60, day)),
                  ("foreign_ratio", lambda: fetch_market.foreign_ratio(ticker, exchange, calendar, 24))]
        if exchange == "TWSE":
            venue.append(("lending", lambda: fetch_market.lending(ticker, days60[-20:], day)))
        group["觀測站"] += [("financial", lambda: fetch_data.financial(ticker, 12, day)),
                         ("revenue", lambda: fetch_data.revenue(ticker, 25, day))]
        group["其他"].append(("holders", lambda: fetch_market.holders(ticker)))
    else:
        group["其他"] += [("etf", lambda: fetch_data.etf(ticker)), ("fx", lambda: fetch_market.fx(MONTHS, day))]
    if info["類型"] == "債券ETF":
        group["其他"].append(("ust", lambda: fetch_market.ust(2, day)))
    with ThreadPoolExecutor(max_workers=len(group)) as pool:
        for future in [pool.submit(lambda job: [run(name, call) for name, call in job], job) for job in group.values()]:
            future.result()
    if "nav" in data:
        data["scale"] = scale_history(ticker, data["nav"])
    adjust_split(data.get("price"), data.get("dividend"))

    for itor in data.values():
        itor.save(folder)
    close = data["price"].row[-1] if data["price"].row else None
    if close:
        info.update({"收盤": close[4], "收盤日": close[0]})
    sigma = volatility([itor[4] for itor in data["price"].row])
    if sigma:
        info["年化波動度"] = round(sigma, 1)
    (folder / "標的.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = Path(work) / "資料摘要.md"
    summary.write_text(summarize(info, data), encoding="utf-8")
    return info, data


def adjust_split(price, dividend=None):
    """偵測股票分割（相鄰交易日收盤價約為 1/k），把分割前的價格、成交量與每股配息換算成分割後的基準。"""
    if not price or len(price.row) < 2:
        return []
    found = []
    for before, after in zip(price.row, price.row[1:]):
        if not before[4] or not after[4]:
            continue
        ratio = before[4] / after[4]
        for k in range(2, 11):
            # 單日漲跌幅限制 10%，比例接近整數倍只可能是分割（或反分割）
            if abs(ratio / k - 1) < 0.12:
                found.append((after[0], k))
            elif abs(ratio * k - 1) < 0.12:
                found.append((after[0], 1 / k))
    for day, k in found:
        for itor in price.row:
            if itor[0] < day:
                itor[1:5] = [round(v / k, 4) if v is not None else None for v in itor[1:5]]
                itor[5] = round(itor[5] * k) if itor[5] is not None else None
        if dividend:
            for itor in dividend.row:
                if itor[0] < day and itor[1] is not None:
                    itor[1] = round(itor[1] / k, 4)
        label = f"1 拆 {k}" if k > 1 else f"{round(1 / k)} 合 1"
        note = f"{day} 起股票{label}，之前的價格、成交量與每股配息已換算成分割後基準"
        price.note = f"{price.note}；{note}" if price.note else note
        if dividend:
            dividend.note = f"{dividend.note}；{note}" if dividend.note else note
    return found


def scale_history(ticker, nav):
    """ETF 流通單位數只有當日值，每次抓取時保存，逐次累積資金流向。"""
    data = Dataset("scale", f"{ticker} 規模歷史", ["日期", "淨值", "流通單位數"], {"淨值": "元", "流通單位數": "單位"},
                   "發行商只提供當日值，歷史由每次研究累積")
    path = CACHE / "規模" / f"{ticker}.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for itor in nav.row:
        if len(itor) >= 3:
            saved[itor[0]] = [itor[1], itor[2]]
    if saved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    data.row = [[day] + value for day, value in sorted(saved.items())]
    data.source = nav.source
    return data


# 摘要

def fmt(value, digit=2):
    if value is None:
        return "—"
    if isinstance(value, float) and abs(value) >= 1000:
        return f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digit}f}"


def pct(value):
    return "—" if value is None else f"{value:+.1f}%"


def change(series, back):
    """series 最新值相對 back 筆以前的變動率（%）。"""
    if len(series) <= back or not series[-1 - back]:
        return None
    return (series[-1] / series[-1 - back] - 1) * 100


def mean(value):
    value = [itor for itor in value if itor is not None]
    return sum(value) / len(value) if value else None


def table(head, row):
    line = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    line += ["| " + " | ".join(str(itor) for itor in r) + " |" for r in row]
    return "\n".join(line)


def quantile(value, q):
    value = sorted(itor for itor in value if itor is not None)
    if not value:
        return None
    return statistics.quantiles(value, n=4)[q - 1] if len(value) >= 4 else value[len(value) // 2]


def volatility(close, days=500):
    """近 2 年日報酬的年化波動度（%），供 check.py 推算情境的基準機率。資料不足 60 天時回傳 None。"""
    close = [c for c in close[-days:] if c]
    if len(close) < 60:
        return None
    change = [math.log(b / a) for a, b in zip(close, close[1:])]
    return statistics.stdev(change) * math.sqrt(250) * 100


def beta(close, index, step=5):
    """近 2 年週報酬相對大盤的 Beta，供股權成本檢查。close、index 為（日期, 收盤）清單；不足 52 週時回傳 None。"""
    level = dict(index)
    pair = [(c, level[d]) for d, c in close if c and level.get(d)][::-1][::step][::-1]
    if len(pair) < 53:
        return None, 0
    stock = [math.log(b[0] / a[0]) for a, b in zip(pair, pair[1:])]
    market = [math.log(b[1] / a[1]) for a, b in zip(pair, pair[1:])]
    return statistics.covariance(stock, market) / statistics.variance(market), len(stock)


def price_section(data):
    row = data["price"].row
    if not row:
        return "## 行情\n\n無資料。"
    close = [itor[4] for itor in row]
    volume = [itor[5] for itor in row]
    last = row[-1]
    year = close[-250:]
    bench = data.get("otc_index") or data.get("taiex")
    slope, weeks = beta([(itor[0], itor[4]) for itor in row[-500:]], [(itor[0], itor[1]) for itor in bench.row])         if bench and bench.row else (None, 0)
    line = [f"## 行情（{data['price'].title}）", "",
            f"- 附註：{data['price'].note}" if data["price"].note else None,
            f"- 最新收盤：{fmt(last[4])} 元（{last[0]}）",
            f"- {'近 52 週' if len(close) >= 250 else f'上市以來（{row[0][0]} 起，{len(close)} 個交易日）'}："
            f"最高 {fmt(max(year))}、最低 {fmt(min(year))}，現價位於區間 "
            f"{(last[4] - min(year)) / (max(year) - min(year)) * 100:.0f}% 處" if max(year) > min(year) else None,
            "- 均線：" + "、".join(f"{n} 日 {fmt(mean(close[-n:]))}" for n in (20, 60, 120, 240) if len(close) >= n),
            "- 報酬：" + "、".join(f"{label} {pct(change(close, n))}"
                                for label, n in (("1 個月", 21), ("3 個月", 63), ("6 個月", 126), ("1 年", 250),
                                                 ("2 年", 500))),
            f"- 年化波動度（近 2 年日報酬）：{fmt(volatility(close), 1)}%" if volatility(close) else None,
            f"- Beta（近 2 年 {weeks} 週報酬，相對{'櫃買指數' if data.get('otc_index') else '加權指數'}）：{fmt(slope)}" if slope is not None else None,
            f"- 成交量：近 20 日均量 {fmt(mean(volume[-20:]), 0)} 張，近 60 日均量 {fmt(mean(volume[-60:]), 0)} 張"]
    return "\n".join(itor for itor in line if itor is not None)


def valuation_section(data):
    if "valuation" not in data or not data["valuation"].row:
        return ""
    row = data["valuation"].row
    span = f"{row[0][0]}～{row[-1][0]}"
    line = [f"## 估值（交易所公布，{span}，最長 5 年）", ""]
    head = ["指標", "最新", "25 百分位", "中位數", "75 百分位", "最低", "最高"]
    body = []
    for index, name in ((1, "本益比"), (2, "股價淨值比"), (3, "殖利率 %")):
        value = [itor[index] for itor in row if itor[index]]
        if value:
            body.append([name, fmt(row[-1][index]), fmt(quantile(value, 1)), fmt(quantile(value, 2)),
                         fmt(quantile(value, 3)), fmt(min(value)), fmt(max(value))])
    line.append(table(head, body))
    line.append(f"\n最新本益比所依據的財報季：{row[-1][4]}")
    return "\n".join(line)


def financial_section(data):
    if "financial" not in data or not data["financial"].row:
        return ""
    row = data["financial"].row
    body = [[r[0], fmt(r[1] / 1e5 if r[1] is not None else None), fmt(r[7]), fmt(r[8]),
             fmt(r[5] / 1e5 if r[5] is not None else None), fmt(r[6])] for r in row]
    eps = [r[6] for r in row[-4:] if r[6] is not None]
    line = ["## 單季財報", "", table(["季", "營收（億元）", "毛利率 %", "營益率 %", "歸母淨利（億元）", "EPS"], body)]
    if len(eps) == 4:
        line.append(f"\n近四季 EPS 合計：{sum(eps):.2f} 元")
    return "\n".join(line)


def revenue_section(data):
    if "revenue" not in data or not data["revenue"].row:
        return ""
    row = data["revenue"].row
    body = [[r[0], fmt(r[1] / 1e5 if r[1] is not None else None), pct(r[3])] for r in row[-13:]]
    line = ["## 月營收（近 13 個月）", "", table(["月", "營收（億元）", "年增率"], body)]
    year = row[-1][0][:4]
    this = [r for r in row if r[0].startswith(year)]
    if this and all(r[2] for r in this):
        total, before = sum(r[1] for r in this), sum(r[2] for r in this)
        line.append(f"\n{year} 年累計 {len(this)} 個月：{total / 1e5:,.2f} 億元，年增 {pct((total / before - 1) * 100)}")
    return "\n".join(line)


def dividend_section(data):
    row = data["dividend"].row if "dividend" in data else []
    if not row:
        return "## 除權息（近 2 年）\n\n無紀錄。"
    return "## 除權息（近 2 年）\n\n" + table(["日期", "權值+息值（元）", "類別"], [[r[0], fmt(r[1], 4), r[2]] for r in row])


def chips_section(data):
    line = ["## 籌碼"]
    if "institutional" in data and data["institutional"].row:
        row = data["institutional"].row
        body = [[name] + [fmt(sum(r[i] or 0 for r in row[-n:]), 0) for n in (5, 20, 60)]
                for i, name in ((1, "外資"), (2, "投信"), (3, "自營商"), (4, "合計"))]
        line += ["", "### 三大法人累計買賣超（張）", "", table(["法人", "近 5 日", "近 20 日", "近 60 日"], body)]
    if "margin" in data and data["margin"].row:
        row = data["margin"].row
        body = []
        for i, name in ((1, "融資餘額"), (2, "融券餘額")):
            value = [r[i] for r in row]
            body.append([name, fmt(value[-1], 0)] + [pct(change(value, min(n, len(value) - 1))) for n in (5, 20, 60)])
        line += ["", f"### 融資融券（{row[-1][0]}，張）", "", table(["項目", "最新", "5 日變動", "20 日變動", "60 日變動"], body)]
    if "lending" in data and data["lending"].row:
        value = [r[1] for r in data["lending"].row]
        line.append(f"\n借券賣出餘額：{fmt(value[-1], 0)} 張，近 20 日變動 {pct(change(value, len(value) - 1))}")
    if "foreign_ratio" in data and data["foreign_ratio"].row:
        row = data["foreign_ratio"].row
        point = [row[-1]] + [row[-1 - n] for n in (6, 12, 23) if len(row) > n]
        line.append("\n外資持股比率：" + "、".join(f"{r[0]} {fmt(r[1])}%" for r in point))
    if "holders" in data and data["holders"].row:
        row = data["holders"].row
        last = row[-1]
        text = (f"\n集保股權分散（{last[0]}）：400 張以上 {fmt(last[1])}%、1000 張以上 {fmt(last[2])}%、"
                f"10 張以下 {fmt(last[3])}%、股東 {fmt(last[4], 0)} 人")
        if len(row) > 1:
            text += f"；最早紀錄 {row[0][0]}：400 張以上 {fmt(row[0][1])}%、股東 {fmt(row[0][4], 0)} 人"
        else:
            text += "（首次紀錄，尚無歷史可比）"
        line.append(text)
    return "\n".join(line) if len(line) > 1 else ""


def market_section(data):
    line = ["## 市場健康度"]
    row = data["taiex"].row
    if row:
        index = [r[1] for r in row]
        value = [r[2] for r in row]
        line += ["", f"- 加權指數 {fmt(index[-1])}（{row[-1][0]}），"
                 + "、".join(f"{n} 日均線 {fmt(mean(index[-n:]))}" for n in (20, 60, 240) if len(index) >= n),
                 "- 加權指數報酬：" + "、".join(f"{label} {pct(change(index, n))}"
                                           for label, n in (("1 個月", 21), ("3 個月", 63), ("6 個月", 126), ("1 年", 250))),
                 f"- 成交金額：近 20 日平均 {fmt(mean(value[-20:]), 0)} 億元，近 60 日平均 {fmt(mean(value[-60:]), 0)} 億元，"
                 f"近 250 日平均 {fmt(mean(value[-250:]), 0)} 億元"]
        close = {r[0]: r[4] for r in data["price"].row}
        pair = [(r[1], close[r[0]]) for r in row if r[0] in close]
        if len(pair) > 126:
            rel = "、".join(f"{label} {pct(((pair[-1][1] / pair[-1 - n][1]) / (pair[-1][0] / pair[-1 - n][0]) - 1) * 100)}"
                           for label, n in (("1 個月", 21), ("3 個月", 63), ("6 個月", 126)))
            line.append(f"- 相對加權指數的超額報酬：{rel}")
    if "otc_index" in data and data["otc_index"].row:
        index = [r[1] for r in data["otc_index"].row]
        line.append(f"- 櫃買指數 {fmt(index[-1])}，60 日均線 {fmt(mean(index[-60:]))}，"
                    f"240 日均線 {fmt(mean(index[-240:]))}，近 3 個月 {pct(change(index, 63))}")
    if "market_margin" in data and data["market_margin"].row:
        value = [r[1] for r in data["market_margin"].row]
        line.append(f"- 上市融資金額 {fmt(value[-1], 0)} 億元，近 20 日 {pct(change(value, 20))}、"
                    f"近 60 日 {pct(change(value, len(value) - 1))}")
    if "futures" in data and data["futures"].row:
        row = data["futures"].row
        line.append(f"- 外資台指期淨未平倉 {fmt(row[-1][1], 0)} 口（{row[-1][0]}），20 個交易日前 "
                    f"{fmt(row[-21][1] if len(row) > 20 else row[0][1], 0)} 口")
    return "\n".join(line)


def etf_section(data):
    line = []
    if "nav" in data and data["nav"].row:
        nav = data["nav"].row[-1]
        close = {r[0]: r[4] for r in data["price"].row}
        text = f"- 淨值 {fmt(nav[1], 4)} 元（{nav[0]}）"
        if nav[0] in close:
            text += f"，同日收盤 {fmt(close[nav[0]])} 元，折溢價 {pct((close[nav[0]] / nav[1] - 1) * 100)}"
        if len(nav) > 3:
            text += f"；流通單位數 {fmt(nav[2], 0)}、淨資產 {fmt(nav[3] / 1e8, 0)} 億元"
        line += ["## 淨值與規模", "", text]
    if "scale" in data and len(data["scale"].row) > 1:
        first, last = data["scale"].row[0], data["scale"].row[-1]
        line.append(f"- 流通單位數：{first[0]} {fmt(first[2], 0)} → {last[0]} {fmt(last[2], 0)}（{pct(change([first[2], last[2]], 1))}）")
    if "holding" in data and data["holding"].row:
        holding = data["holding"]
        stock = [r for r in holding.row if r[3] is not None]
        line += ["", f"## 持股（{holding.source[0].get('資料日', '')}）", "",
                 f"前五大合計 {fmt(sum(r[3] for r in stock[:5]))}%、前十大合計 {fmt(sum(r[3] for r in stock[:10]))}%，"
                 f"共 {len(stock)} 筆。{holding.note or ''}", "",
                 table(["類別", "代號", "名稱", "權重 %"], [[r[0], r[1], r[2], fmt(r[3])] for r in stock[:15]])]
        if len(holding.column) > 5:
            for index, name in ((4, "產業"), (5, "地區")):
                group = {}
                for r in stock:
                    group[r[index]] = group.get(r[index], 0) + r[3]
                top = sorted(group.items(), key=lambda itor: -itor[1])[:10]
                line.append(f"\n{name}權重：" + "、".join(f"{k} {v:.1f}%" for k, v in top))
    return "\n".join(line)


def macro_section(data):
    line = []
    if "ust" in data and data["ust"].row:
        row = data["ust"].row
        line += ["## 美國公債殖利率", ""]
        for index, name in ((3, "20 年"), (4, "30 年"), (2, "10 年"), (1, "2 年")):
            value = [r[index] for r in row if r[index] is not None]
            half = [r[index] for r in row[-126:] if r[index] is not None]
            line.append(f"- {name}：{fmt(value[-1])}%（{row[-1][0]}），近 6 個月 {fmt(min(half))}～{fmt(max(half))}%，"
                        f"近 2 年 {fmt(min(value))}～{fmt(max(value))}%，1 年前 {fmt(value[-250] if len(value) >= 250 else value[0])}%")
    if "fx" in data and data["fx"].row:
        value = [r[1] for r in data["fx"].row]
        line += ["", "## 美元兌新臺幣", "",
                 f"- 最新 {fmt(value[-1], 3)}（{data['fx'].row[-1][0]}），近 6 個月 {fmt(min(value[-126:]), 3)}～"
                 f"{fmt(max(value[-126:]), 3)}，近 2 年 {fmt(min(value), 3)}～{fmt(max(value), 3)}"]
    return "\n".join(line)


def summarize(info, data):
    head = [f"# {info['代號']} {info['名稱']} 資料摘要\n\n"
            f"- 類型：{info['類型']}；交易所：{info['交易所']}；研究日：{info['研究日']}\n"
            f"- 本檔由 `工具/collect.py` 產生，完整資料在 `資料/*.json`。數字以本檔與 `資料/` 為準。"]
    part = [price_section(data), valuation_section(data), financial_section(data), revenue_section(data),
            dividend_section(data), etf_section(data), chips_section(data), market_section(data), macro_section(data)]
    missing = [f"- {name}：{'；'.join(itor.missing[:5])}{' 等' if len(itor.missing) > 5 else ''}"
               for name, itor in data.items() if itor.missing]
    if missing:
        part.append("## 缺漏\n\n" + "\n".join(missing))
    return "\n\n".join(head + [itor for itor in part if itor]) + "\n"


def cli():
    parser = argparse.ArgumentParser(description="抓齊單一標的的研究資料")
    parser.add_argument("ticker")
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=now())
    arg = parser.parse_args()
    info, data = collect(arg.ticker, arg.work, arg.date)
    print(f"完成：{arg.work / '資料摘要.md'}，{len(data)} 份資料")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cli()
