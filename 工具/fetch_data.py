"""抓取證交所、櫃買中心與公開資訊觀測站的固定資料，輸出研究資料可直接引用的來源、輸入與公式。"""
import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

TZ = timezone(timedelta(hours=8))
AGENT = "Mozilla/5.0 (compatible; Finance research tool)"
# 證交所與觀測站會封鎖連續快速查詢，同一主機的請求至少間隔數秒。
PAUSE = 3
MOPS = "https://mopsov.twse.com.tw"
EXCHANGE_NAME = {"TWSE": "證交所", "TPEx": "櫃買中心"}
MARKET_NAME = {"sii": "上市", "otc": "上櫃"}
# 彙總表欄名依產業別不同，依序取第一個存在的欄位。
FIELD = {
    "revenue": ("營業收入", "收益", "淨收益"),
    "gross_profit": ("營業毛利（毛損）淨額", "營業毛利（毛損）"),
    "operating_income": ("營業利益（損失）", "營業利益"),
    "pretax_income": ("稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）", "繼續營業單位稅前損益"),
    "net_income_parent": ("淨利（淨損）歸屬於母公司業主", "淨利（損）歸屬於母公司業主"),
    "eps": ("基本每股盈餘（元）",),
}
LABEL = {"revenue": "營業收入", "gross_profit": "營業毛利", "operating_income": "營業利益",
         "pretax_income": "稅前淨利", "net_income_parent": "歸屬母公司淨利", "eps": "基本每股盈餘"}
EPS_NOTE = "以累計基本每股盈餘相減推算單季，期間股數變動時與公告單季數可能略有差異"
YUANTA_API = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
BLACKROCK = {"009826": "351824"}  # 代號對應貝萊德產品頁編號

_cache = {}
_last = {}


def fetch(url, form=None, encoding="utf-8"):
    key = (url, json.dumps(form, sort_keys=True))
    if key in _cache:
        return _cache[key]
    host = urlsplit(url).hostname
    body = urlencode(form).encode() if form else None
    # 觀測站尖峰時偶爾回 5xx 或連線逾時，間隔後重試；其他錯誤直接回報。
    for attempt in range(3):
        wait = _last.get(host, -PAUSE) + PAUSE * (1 + 3 * attempt) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            with urlopen(Request(url, data=body, headers={"User-Agent": AGENT}), timeout=90) as response:
                text = response.read().decode(encoding, errors="replace")
            break
        except HTTPError as error:
            if error.code == 404:
                text = ""
                break
            if error.code < 500 or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        finally:
            _last[host] = time.monotonic()
    _cache[key] = text
    return text


def number(text):
    text = str(text).replace(",", "").strip()
    if text in ("", "-", "--", "N/A"):
        return None
    value = float(text)
    return int(value) if re.fullmatch(r"-?\d+", text) else value


def roc_date(text):
    """解析民國或西元的年月日文字。"""
    year, month, day = map(int, re.findall(r"\d+", text)[:3])
    return date(year + 1911 if year < 1911 else year, month, day)


def cell(html):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", html))).replace("\xa0", "")


def quarter_end(year, season):
    return date(year, season * 3, 31 if season in (1, 4) else 30)


def previous(year, season):
    return (year, season - 1) if season > 1 else (year - 1, 4)


def recent_quarter(today, count):
    """回傳 today 之前已結束的最近 count 季，由舊到新。"""
    year, season = previous(today.year, (today.month - 1) // 3 + 1)
    result = []
    for _ in range(count):
        result.append((year, season))
        year, season = previous(year, season)
    return result[::-1]


def previous_month(month):
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


class Summary:
    def __init__(self, work, command, ticker):
        self.work = Path(work)
        # 代號多時串接全部會超過 Windows 路徑長度上限，改用首檔代號加檔數
        self.name = f"{command}-{'_'.join(ticker) if len(ticker) <= 3 else f'{ticker[0]}-{len(ticker)}檔'}"
        self.prefix = "" if len(ticker) == 1 else "{}_"
        self.source, self.input, self.formula, self.missing = {}, {}, {}, []
        self.evidence, self.table = {}, {}

    def key(self, ticker, name):
        return self.prefix.format(ticker) + name

    def add_source(self, key, name, url, data_date, field, row, locator=None):
        entry = {"name": name, "url": url, "data_date": data_date.isoformat()}
        if locator:
            entry["locator"] = locator
        self.source[key] = entry
        # 只保存用到的列，讓審查可核對又不必保存整份全市場回應；同一來源供多檔使用時累積各列。
        evidence = self.evidence.setdefault(key, {**entry, "fields": field, "rows": []})
        evidence["rows"] += [itor for itor in row if itor not in evidence["rows"]]

    def add_input(self, key, value, unit, day, source, label, note=None, precision=2):
        entry = {"value": value, "unit": unit, "kind": "verified", "date": day.isoformat(),
                 "source": [source], "label": label, "precision": precision}
        if note:
            entry["note"] = note
        self.input[key] = entry

    def save(self):
        fetched = datetime.now(TZ).isoformat(timespec="seconds")
        for key, evidence in self.evidence.items():
            path = self.work / "來源" / f"{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            # 同一來源可能分次抓取不同標的（例如先抓 50 檔、再補金控），保留舊檔中本次未抓到的列
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if old.get("fields") == evidence["fields"]:
                    fresh = {row[0] for row in evidence["rows"]}
                    evidence = {**evidence, "rows": [row for row in old["rows"] if row[0] not in fresh] + evidence["rows"]}
            path.write_text(json.dumps({**evidence, "fetched_at": fetched}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        path = self.work / "抓取" / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = {"source": self.source, "input": self.input, "formula": self.formula,
                   "table": self.table, "missing": self.missing}
        path.write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


# 行情與本益比

def day_url(exchange, ticker, month):
    if exchange == "TWSE":
        return f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={month:%Y%m}01&stockNo={ticker}&response=json"
    return f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={ticker}&date={month:%Y/%m}/01&response=json"


def pe_url(exchange, ticker, month):
    if exchange == "TWSE":
        return f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU?date={month:%Y%m}01&stockNo={ticker}&response=json"
    return f"https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryStock?code={ticker}&date={month:%Y/%m}/01&response=json"


def table_rows(exchange, text):
    if not text.strip():
        return []
    data = json.loads(text)
    if exchange == "TWSE":
        return data.get("data", []) if data.get("stat") == "OK" else []
    table = data.get("tables") or [{}]
    return table[0].get("data", [])


def detect(ticker, today):
    month = previous_month(today)
    for exchange in ("TWSE", "TPEx"):
        if table_rows(exchange, fetch(day_url(exchange, ticker, month))):
            return exchange
    raise ValueError(f"證交所與櫃買中心都查不到 {ticker}，興櫃或新上市標的請另行查詢")


def on_or_before(exchange, url, target):
    """回傳 target 當天或之前最後一個交易日的列，該月沒有時往前找一個月。"""
    for month in (target.replace(day=1), previous_month(target)):
        link = url(month)
        row = [itor for itor in table_rows(exchange, fetch(link)) if roc_date(itor[0]) <= target]
        if row:
            return month, link, row[-1]
    return None, None, None


def price(summary, ticker, exchange, quarters, today):
    target = [(f"{year}q{season}", quarter_end(year, season))
              for year, season in recent_quarter(today, quarters)] + [("latest", today)]
    venue = EXCHANGE_NAME[exchange]
    # 證交所本益比在第 4 欄、櫃買中心在第 2 欄，財報年季都在第 6 欄。
    pe_index = 3 if exchange == "TWSE" else 1
    for label, day in target:
        month, link, row = on_or_before(exchange, lambda m: day_url(exchange, ticker, m), day)
        if row is None:
            summary.missing.append(f"{label} 收盤價")
            continue
        trade = roc_date(row[0])
        key = f"{exchange.lower()}_day_{ticker}_{month:%Y%m}"
        summary.add_source(key, f"{venue}｜{ticker} {month:%Y-%m} 個股日成交資訊", link, trade,
                           ["日期", "成交量", "成交金額", "開盤", "最高", "最低", "收盤"], [row[:7]])
        summary.add_input(summary.key(ticker, f"close_{label}"), number(row[6]), "元", trade, key,
                          f"{ticker} {trade} 收盤價")
        month, link, row = on_or_before(exchange, lambda m: pe_url(exchange, ticker, m), day)
        if row is None or number(row[pe_index]) is None:
            summary.missing.append(f"{label} 本益比（ETF 或虧損時交易所不提供）")
            continue
        trade = roc_date(row[0])
        key = f"{exchange.lower()}_pe_{ticker}_{month:%Y%m}"
        summary.add_source(key, f"{venue}｜{ticker} {month:%Y-%m} 個股本益比", link, trade,
                           ["日期", "本益比", "財報年/季"], [[row[0], row[pe_index], row[5]]])
        summary.add_input(summary.key(ticker, f"pe_{label}"), number(row[pe_index]), "倍", trade, key,
                          f"{ticker} {trade} 本益比", f"交易所以財報 {row[5]} 的近四季每股盈餘計算")


def dividend(summary, ticker, exchange, since, today):
    if exchange == "TWSE":
        link = (f"https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={since:%Y%m%d}"
                f"&endDate={today:%Y%m%d}&response=json")
        column = {"value": 5, "type": 6}
    else:
        link = (f"https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={since:%Y/%m/%d}"
                f"&endDate={today:%Y/%m/%d}&response=json")
        column = {"value": 7, "type": 8}
    row = [itor for itor in table_rows(exchange, fetch(link)) if itor[1].strip() == ticker]
    if not row:
        summary.missing.append(f"{since} 起的除權息紀錄")
        return
    key = f"{exchange.lower()}_exright_{ticker}"
    summary.add_source(key, f"{EXCHANGE_NAME[exchange]}｜{ticker} 除權除息計算結果", link, today,
                       ["日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息"],
                       [itor[:4] + [itor[4], itor[column["value"]], itor[column["type"]]] for itor in row])
    for itor in row:
        day = roc_date(itor[0])
        kind = itor[column["type"]].strip()
        summary.add_input(summary.key(ticker, f"exright_{day:%Y%m%d}"), number(itor[column["value"]]), "元",
                          day, key, f"{ticker} {day} {kind}值", f"{kind}；權值＋息值，除息時即每單位現金分配",
                          precision=4)


# 財報與月營收

def parse_statement(html):
    """解析綜合損益表彙總，回傳 {公司代號: {欄名: 文字}}。"""
    result = {}
    for table in re.findall(r"<table class=.hasBorder.*?</table>", html, re.S | re.I):
        head = [cell(itor) for itor in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S | re.I)]
        for row in re.findall(r"<tr class=.(?:even|odd).[^>]*>(.*?)</tr>", table, re.S | re.I):
            value = [cell(itor) for itor in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
            if len(value) == len(head):
                result[value[0]] = dict(zip(head, value))
    return result


def statement(typek, year, season):
    form = {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "isQuery": "Y",
            "TYPEK": typek, "year": year - 1911, "season": f"{season:02d}"}
    url = f"{MOPS}/mops/web/ajax_t163sb04"
    for attempt in range(2):
        html = fetch(url, form)
        if "查詢過於頻繁" in html or "因為安全性考量" in html:
            raise ValueError("公開資訊觀測站暫時拒絕查詢，請稍後再試")
        row = parse_statement(html)
        if row:
            return row
        # 觀測站偶爾對已公告的季度回空白頁，清除快取重試一次，避免整季被誤判為未公告
        _cache.pop((url, json.dumps(form, sort_keys=True)), None)
    return row


_market = {}


def find_statement(ticker, year, season):
    # 每份彙總表約 1–2 MB，先查已知市場以減少請求。
    for typek in sorted(("sii", "otc"), key=lambda itor: itor != _market.get(ticker)):
        row = statement(typek, year, season).get(ticker)
        if row:
            _market[ticker] = typek
            return typek, row
    return None, None


def financial(summary, ticker, quarters, today):
    period = recent_quarter(today, quarters + 2)
    # 最新一季可能尚未公告，全部查不到才捨棄；金控第一、三季公告較晚，部分缺漏改列 missing。
    if all(find_statement(itor, *period[-1])[1] is None for itor in ticker):
        period = period[:-1]
    period = period[-quarters:]
    if period[0][1] > 1:
        period = [previous(*period[0])] + period
    for code in ticker:
        found = set()
        for year, season in period:
            typek, row = find_statement(code, year, season)
            if row is None:
                summary.missing.append(f"{code} {year}Q{season} 綜合損益表")
                continue
            found.add((year, season))
            key = f"mops_is_{typek}_{year}q{season}"
            column = {name: next((itor for itor in FIELD[name] if itor in row), None) for name in FIELD}
            entry = {name: row[label] for name, label in column.items() if label}
            summary.add_source(key, f"公開資訊觀測站｜綜合損益表彙總（{MARKET_NAME[typek]}，{year} 年第 {season} 季累計）",
                               f"{MOPS}/mops/web/t163sb04", quarter_end(year, season),
                               ["公司代號"] + list(entry), [[code] + list(entry.values())],
                               f"查詢條件：{MARKET_NAME[typek]}、民國 {year - 1911} 年第 {season} 季；"
                               "金額單位仟元，每股盈餘單位元，皆為年初至該季累計")
            for name, text in entry.items():
                unit, precision = ("元", 2) if name == "eps" else ("仟元", 0)
                ytd = summary.key(code, f"{name}_ytd_{year}q{season}" if season > 1 else f"{name}_{year}q1")
                summary.add_input(ytd, number(text), unit, quarter_end(year, season), key,
                                  f"{code} {year}Q{season} {LABEL[name]}" + ("（累計）" if season > 1 else ""),
                                  precision=precision)
                before = previous(year, season)
                if season > 1 and before in found:
                    prior = summary.key(code, f"{name}_ytd_{before[0]}q{before[1]}" if before[1] > 1
                                        else f"{name}_{before[0]}q1")
                    if prior in summary.input:
                        formula = {"op": "subtract", "args": [ytd, prior], "unit": unit, "precision": precision,
                                   "label": f"{code} {year}Q{season} 單季{LABEL[name]}"}
                        if name == "eps":
                            formula["note"] = EPS_NOTE
                        summary.formula[summary.key(code, f"{name}_{year}q{season}")] = formula


def revenue_page(typek, month, foreign):
    link = f"{MOPS}/nas/t21/{typek}/t21sc03_{month.year - 1911}_{month.month}_{1 if foreign else 0}.html"
    return link, fetch(link, encoding="cp950")


def parse_revenue(html, ticker):
    # 月營收頁面混用 <td> 與 <Td>。
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        value = [cell(itor) for itor in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(value) >= 10 and value[0] == ticker:
            return value
    return None


def find_revenue(ticker, month):
    for typek in ("sii", "otc"):
        for foreign in (False, True):
            link, html = revenue_page(typek, month, foreign)
            row = parse_revenue(html, ticker)
            if row:
                return typek, link, row
    return None, None, None


def revenue(summary, ticker, months, today):
    latest = previous_month(today)
    # 月營收於次月 10 日前公告，所有標的都查得到才當作最新月。
    if any(find_revenue(itor, latest)[2] is None for itor in ticker):
        latest = previous_month(latest)
    period = [latest]
    while len(period) < months:
        period.insert(0, previous_month(period[0]))
    for code in ticker:
        for month in period:
            typek, link, row = find_revenue(code, month)
            if row is None:
                summary.missing.append(f"{code} {month:%Y-%m} 月營收")
                continue
            key = f"mops_revenue_{code}_{month:%Y%m}"
            end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            summary.add_source(key, f"公開資訊觀測站｜{MARKET_NAME[typek]}公司 {month:%Y-%m} 營業收入統計", link, end,
                               ["公司代號", "公司名稱", "當月營收", "上月營收", "去年當月營收", "上月比較增減(%)",
                                "去年同月增減(%)", "當月累計營收", "去年累計營收", "前期比較增減(%)"], [row[:10]])
            summary.add_input(summary.key(code, f"revenue_m{month:%Y%m}"), number(row[2]), "仟元", end, key,
                              f"{code} {month:%Y-%m} 營業收入", precision=0)


# ETF 淨值與持股

def yuanta(summary, ticker):
    param = {"APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "PageName": f"/tradeInfo/pcf/{ticker}",
             "DeviceId": "null", "AppName": "ETF", "Device": 3, "Platform": "ETF",
             "FuncId": "PCF/Daily", "ticker": ticker}
    text = fetch(f"{YUANTA_API}?{urlencode(param)}")
    data = json.loads(text) if text.strip().startswith("{") else {}
    pcf = data.get("PCF") or {}
    if not pcf.get("nav"):
        raise ValueError(f"元大 ETF 資料查不到 {ticker}，目前只支援元大 ETF 與 {'、'.join(BLACKROCK)}")
    day = datetime.strptime(pcf["trandate"], "%Y%m%d").date()
    weight = data.get("FundWeights") or {}
    stock = weight.get("StockWeights") or []
    future = weight.get("FutureWeights") or []
    bond = weight.get("BondWeights") or []
    key = f"yuanta_pcf_{ticker}_{day:%Y%m%d}"
    row = ([["股票", itor["code"], itor["name"], itor["weights"], itor["qty"]] for itor in stock]
           + [["期貨", f"{itor['code']}{itor['ym']}", itor["name"], itor["weights"], itor["qty"]] for itor in future]
           + [["債券", itor["code"], itor["name"], itor["weights"], itor.get("FACE_AMT")] for itor in bond])
    summary.add_source(key, f"元大投信｜{ticker} 申購買回清單與基金持股", f"https://www.yuantaetfs.com/tradeInfo/pcf/{ticker}",
                       day, ["類別", "代號", "名稱", "權重(%)", "股數／口數／面額"], row,
                       f"元大 ETF 資料介面 PCF/Daily，{day} 淨值，更新時間 {pcf['upddate']}；金額單位新臺幣元")
    summary.add_input("nav", pcf["nav"], "元", day, key, f"{ticker} {day} 正式淨值", precision=4)
    summary.add_input("units", pcf["osunit"], "單位", day, key, f"{ticker} 流通單位數", precision=0)
    summary.add_input("net_assets", pcf["totalav"], "元", day, key, f"{ticker} 基金淨資產", precision=0)
    total = weight.get("Summary") or {}
    for name, label, text in (("stkvalues", "stock_value", "股票"), ("futvalues", "future_value", "期貨"),
                              ("bndvalues", "bond_value", "債券")):
        if total.get(name):
            summary.add_input(label, total[name], "元", day, key, f"{ticker} {text}部位市值", precision=0)
    # 股票與期貨權重合計約 100%，且股票權重合計等於股票市值÷（股票市值＋期貨名目），可知分母不是基金淨資產
    note = "元大表列權重，分母為股票市值加期貨名目金額，不是基金淨資產"
    for itor in stock:
        code = itor["code"]
        summary.add_input(f"holding_qty_{code}", itor["qty"], "股", day, key, f"{itor['name']} 持股數", precision=0)
        summary.add_input(f"holding_weight_{code}", itor["weights"], "%", day, key, f"{itor['name']} 權重", note)
        summary.formula[f"shares_per_unit_{code}"] = {
            "op": "divide", "args": [f"holding_qty_{code}", "units"], "unit": "股", "precision": 6,
            "label": f"每單位 {itor['name']} 股數"}
    for itor in future:
        summary.add_input(f"future_weight_{itor['code']}{itor['ym']}", itor["weights"], "%", day, key,
                          f"{itor['name']} {itor['ym']} 權重", note)
    for itor in bond:
        summary.add_input(f"bond_weight_{itor['code']}", itor["weights"], "%", day, key, f"{itor['name']} 權重", note)


def csv_row(text):
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def blackrock(summary, ticker, product, top):
    base = f"https://www.blackrock.com/tw/products/{product}/fund"
    link = f"{base}/1489751333078.ajax?fileType=csv&fileName=historical-nav&dataType=nav-component-table"
    row = [itor for itor in csv_row(fetch(link)) if itor]
    day = roc_date(row[1][0])
    key = f"blackrock_nav_{ticker}"
    summary.add_source(key, f"貝萊德投信｜{ticker} 淨值明細", link, day, row[0], row[1:6])
    summary.add_input("nav", number(row[1][-1]), "元", day, key, f"{ticker} {day} 正式淨值", precision=4)

    link = f"{base}/1480664180144.ajax?dataType=fund&fileName={ticker}_holdings&fileType=csv"
    row = csv_row(fetch(link))
    day = roc_date(row[0][1])
    start = next(index for index, itor in enumerate(row) if itor and itor[0] == "Ticker")
    head = row[start]
    holding = [itor for itor in row[start + 1:] if len(itor) == len(head)]
    key = f"blackrock_holding_{ticker}"
    # 保存全部持股，地區與產業加總才能由審查重算。
    summary.add_source(key, f"貝萊德投信｜{ticker} 基金投資組合", link, day, head, holding,
                       "市值單位新臺幣元，權重分母為基金淨資產")
    column = {name: head.index(name) for name in ("Ticker", "Name", "Sector", "Asset Class", "Weight (%)", "Location")}
    for name, label in (("Location", "location"), ("Sector", "sector")):
        group = {}
        for itor in holding:
            group[itor[column[name]]] = group.get(itor[column[name]], 0) + number(itor[column["Weight (%)"]])
        summary.table[label] = sorted(([k, round(v, 2)] for k, v in group.items()), key=lambda itor: -itor[1])
    equity = [itor for itor in holding if itor[column["Asset Class"]] not in ("Cash", "FX", "Futures")]
    equity.sort(key=lambda itor: -number(itor[column["Weight (%)"]]))
    for itor in equity[:top]:
        code = re.sub(r"[^A-Za-z0-9]", "-", itor[column["Ticker"]])
        if f"holding_weight_{code}" not in summary.input:
            summary.add_input(f"holding_weight_{code}", number(itor[column["Weight (%)"]]), "%", day, key,
                              f"{itor[column['Name']]} 權重")


def etf(summary, ticker, top):
    if ticker in BLACKROCK:
        blackrock(summary, ticker, BLACKROCK[ticker], top)
    else:
        yuanta(summary, ticker)


def cli():
    parser = argparse.ArgumentParser(description="Finance 固定資料抓取工具")
    command = parser.add_subparsers(dest="command", required=True)
    for name, multiple in (("price", False), ("dividend", False), ("financial", True), ("revenue", True),
                           ("etf", False)):
        sub = command.add_parser(name)
        sub.add_argument("--ticker", nargs="+" if multiple else None, required=True)
        sub.add_argument("--work", required=True, type=Path, help="工作夾，原始證據存入 來源/，摘要存入 抓取/")
        sub.add_argument("--date", type=date.fromisoformat, default=datetime.now(TZ).date(),
                         help="以此日期為基準，預設今天")
        if name in ("price", "dividend"):
            sub.add_argument("--exchange", choices=("TWSE", "TPEx"))
    command.choices["price"].add_argument("--quarters", type=int, default=12)
    command.choices["dividend"].add_argument("--since", type=date.fromisoformat)
    command.choices["financial"].add_argument("--quarters", type=int, default=12)
    command.choices["revenue"].add_argument("--months", type=int, default=13)
    command.choices["etf"].add_argument("--top", type=int, default=20, help="貝萊德 ETF 列出的前幾大持股")
    arg = parser.parse_args()

    ticker = arg.ticker if isinstance(arg.ticker, list) else [arg.ticker]
    summary = Summary(arg.work, arg.command, ticker)
    if arg.command == "etf":
        etf(summary, ticker[0], arg.top)
    elif arg.command in ("price", "dividend"):
        exchange = arg.exchange or detect(ticker[0], arg.date)
        if arg.command == "price":
            price(summary, ticker[0], exchange, arg.quarters, arg.date)
        else:
            dividend(summary, ticker[0], exchange, arg.since or arg.date.replace(year=arg.date.year - 2), arg.date)
    elif arg.command == "financial":
        financial(summary, ticker, arg.quarters, arg.date)
    else:
        revenue(summary, ticker, arg.months, arg.date)
    path = summary.save()

    line = [f"摘要：{path}", f"來源 {len(summary.source)} 筆、輸入 {len(summary.input)} 筆、公式 {len(summary.formula)} 筆"]
    value = [f"{k}\t{v['value']} {v['unit']}\t{v['date']}" for k, v in summary.input.items()]
    value += [f"{k}\t= {v['op']}({', '.join(map(str, v['args']))})" for k, v in summary.formula.items()]
    # 數值多時只顯示前段，避免把整份持股或多檔財報帶進對話。
    line += value if len(value) <= 120 else value[:30] + [f"其餘 {len(value) - 30} 筆見摘要檔"]
    for k, v in summary.table.items():
        line.append(f"{k} 權重（前 10）：" + "、".join(f"{name} {value}%" for name, value in v[:10]))
    line += [f"缺漏：{itor}" for itor in summary.missing]
    return "\n".join(line)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        print(cli())
    except (OSError, ValueError, KeyError, IndexError) as error:
        sys.exit(f"資料抓取失敗：{error}")
