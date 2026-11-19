"""抓取個股與 ETF 的行情、估值、股利、財報、月營收與持股。"""
import csv
import io
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from source import (Dataset, cell, fetch, fetch_json, forget, number, previous_month, previous_quarter,
                    quarter_end, recent_month, recent_quarter, roc_date, twse_rows)

MOPS = "https://mopsov.twse.com.tw"
MARKET_NAME = {"sii": "上市", "otc": "上櫃"}
EXCHANGE_NAME = {"TWSE": "證交所", "TPEx": "櫃買中心"}
# 彙總表欄名依產業別不同，依序取第一個存在的欄位。
FIELD = {
    "營收": ("營業收入", "收益", "淨收益"),
    "毛利": ("營業毛利（毛損）淨額", "營業毛利（毛損）"),
    "營業利益": ("營業利益（損失）", "營業利益"),
    "稅前淨利": ("稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）", "繼續營業單位稅前損益"),
    "歸母淨利": ("淨利（淨損）歸屬於母公司業主", "淨利（損）歸屬於母公司業主"),
    "EPS": ("基本每股盈餘（元）",),
}
YUANTA_API = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
BLACKROCK = {"009826": "351824"}  # 代號對應貝萊德產品頁編號


def past(month, day):
    """month 所在月份在 day 之前已結束，內容不會再變。"""
    return month.replace(day=1) < day.replace(day=1)


def has_rows(text):
    try:
        return bool(twse_rows(json.loads(text))[1])
    except ValueError:
        return False


# 行情與估值

def day_url(exchange, ticker, month):
    if exchange == "TWSE":
        return f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={month:%Y%m}01&stockNo={ticker}&response=json"
    return f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={ticker}&date={month:%Y/%m}/01&response=json"


def pe_url(exchange, ticker, month):
    if exchange == "TWSE":
        return f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU?date={month:%Y%m}01&stockNo={ticker}&response=json"
    return f"https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryStock?code={ticker}&date={month:%Y/%m}/01&response=json"


def month_rows(url, month, day):
    text = fetch(url, keep=has_rows if past(month, day) else False)
    return twse_rows(json.loads(text))[1] if text.strip().startswith("{") else []


def detect(ticker, day):
    for exchange in ("TWSE", "TPEx"):
        for month in (day.replace(day=1), previous_month(day)):
            if month_rows(day_url(exchange, ticker, month), month, day):
                return exchange
    raise ValueError(f"證交所與櫃買中心都查不到 {ticker}，興櫃或新上市標的請另行查詢")


def daily(ticker, exchange, months, day):
    """日 K：近 months 個月的開高低收與成交量（張）。"""
    data = Dataset("price", f"{ticker} 日成交", ["日期", "開盤", "最高", "最低", "收盤", "成交量"],
                   {"開盤": "元", "收盤": "元", "成交量": "張"})
    for month in recent_month(day, months):
        url = day_url(exchange, ticker, month)
        row = month_rows(url, month, day)
        if not row:
            data.missing.append(f"{month:%Y-%m} 日成交")
            continue
        data.add_source(f"{EXCHANGE_NAME[exchange]}｜{ticker} 個股日成交資訊", url.split("?")[0])
        for itor in row:
            volume = number(itor[1])
            # 證交所成交量單位為股，櫃買中心為張
            if exchange == "TWSE" and volume is not None:
                volume = round(volume / 1000)
            data.row.append([roc_date(itor[0]).isoformat(), number(itor[3]), number(itor[4]), number(itor[5]),
                             number(itor[6]), volume])
    data.row = [itor for itor in data.row if itor[4] is not None and itor[0] <= day.isoformat()]
    return data


def valuation(ticker, exchange, months, day):
    """交易所每日公布的本益比、股價淨值比與殖利率。ETF 沒有這項資料。"""
    data = Dataset("valuation", f"{ticker} 本益比、股價淨值比、殖利率",
                   ["日期", "本益比", "股價淨值比", "殖利率", "財報季"], {"本益比": "倍", "股價淨值比": "倍", "殖利率": "%"},
                   "交易所以最近四季每股盈餘計算本益比，虧損時不提供")
    # 證交所欄位：日期、殖利率、股利年度、本益比、股價淨值比、財報年/季；櫃買：日期、本益比、殖利率、股利年度、股價淨值比、財報年季
    pick = (3, 4, 1, 5) if exchange == "TWSE" else (1, 4, 2, 5)
    for month in recent_month(day, months):
        url = pe_url(exchange, ticker, month)
        row = month_rows(url, month, day)
        if not row:
            data.missing.append(f"{month:%Y-%m} 本益比")
            continue
        data.add_source(f"{EXCHANGE_NAME[exchange]}｜{ticker} 個股本益比、殖利率及股價淨值比", url.split("?")[0])
        for itor in row:
            if len(itor) > max(pick):
                data.row.append([roc_date(itor[0]).isoformat()] + [number(itor[i]) for i in pick[:3]] + [str(itor[pick[3]])])
    return data


def dividend(ticker, exchange, since, day):
    if exchange == "TWSE":
        url = (f"https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={since:%Y%m%d}"
               f"&endDate={day:%Y%m%d}&response=json")
        column = (5, 6)
    else:
        url = (f"https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={since:%Y/%m/%d}"
               f"&endDate={day:%Y/%m/%d}&response=json")
        column = (7, 8)
    data = Dataset("dividend", f"{ticker} 除權息", ["日期", "權值+息值", "類別"], {"權值+息值": "元"},
                   "除息時權值＋息值即每單位現金分配")
    text = fetch(url)
    row = [itor for itor in twse_rows(json.loads(text))[1] if itor[1].strip() == ticker] if text.strip() else []
    if not row:
        data.missing.append(f"{since} 起的除權息紀錄")
    else:
        data.add_source(f"{EXCHANGE_NAME[exchange]}｜除權除息計算結果", url.split("?")[0], day)
    for itor in row:
        data.row.append([roc_date(itor[0]).isoformat(), number(itor[column[0]]), itor[column[1]].strip()])
    return data


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
    row = {}
    for _ in range(2):
        html = fetch(url, form, keep=lambda text: bool(parse_statement(text)))
        if "查詢過於頻繁" in html or "因為安全性考量" in html:
            raise ValueError("公開資訊觀測站暫時拒絕查詢，請稍後再試")
        row = parse_statement(html)
        if row:
            return row
        # 觀測站偶爾對已公告的季度回空白頁，清除快取重試一次，避免整季被誤判為未公告
        forget(url, form)
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


def financial(ticker, quarters, day):
    """單季損益：觀測站彙總表是年初至該季累計，相減推算單季。金額單位仟元。"""
    data = Dataset("financial", f"{ticker} 單季損益", ["季"] + list(FIELD) + ["毛利率", "營益率"],
                   {"營收": "仟元", "毛利": "仟元", "營業利益": "仟元", "稅前淨利": "仟元", "歸母淨利": "仟元",
                    "EPS": "元", "毛利率": "%", "營益率": "%"},
                   "單季數由累計數相減推算，期間股數變動時，單季 EPS 與公告數可能略有差異")
    period = recent_quarter(day, quarters + 1)
    # 最新一季可能尚未公告
    if find_statement(ticker, *period[-1])[1] is None:
        data.missing.append(f"{period[-1][0]}Q{period[-1][1]} 財報尚未公告")
        period = period[:-1]
    period = period[-quarters:]
    # 往前補到該年第一季，才能用累計數推算單季
    first = period[0]
    while first[1] > 1:
        first = previous_quarter(*first)
        period.insert(0, first)
    ytd = {}
    for year, season in period:
        typek, row = find_statement(ticker, year, season)
        if row is None:
            data.missing.append(f"{year}Q{season} 綜合損益表")
            continue
        data.add_source(f"公開資訊觀測站｜綜合損益表彙總（{MARKET_NAME[typek]}）", f"{MOPS}/mops/web/t163sb04")
        ytd[(year, season)] = {name: number(next((row[k] for k in label if k in row), None)) for name, label in FIELD.items()}
    show = set(period[-quarters:])
    for year, season in period:
        if (year, season) not in ytd or (year, season) not in show:
            continue
        current = ytd[(year, season)]
        if season == 1:
            single = dict(current)
        elif previous_quarter(year, season) in ytd:
            before = ytd[previous_quarter(year, season)]
            single = {k: None if current[k] is None or before[k] is None else round(current[k] - before[k], 2)
                      for k in FIELD}
        else:
            data.missing.append(f"{year}Q{season} 缺少前一季累計數，無法推算單季")
            continue
        revenue = single["營收"]
        margin = [round(single[k] / revenue * 100, 2) if revenue and single[k] is not None else None
                  for k in ("毛利", "營業利益")]
        data.row.append([f"{year}Q{season}"] + [single[k] for k in FIELD] + margin)
    return data


def revenue_page(typek, month, foreign, day):
    url = f"{MOPS}/nas/t21/{typek}/t21sc03_{month.year - 1911}_{month.month}_{1 if foreign else 0}.html"
    return url, fetch(url, encoding="cp950", keep=(lambda text: "公司代號" in text) if past(month, day) else False)


def parse_revenue(html, ticker):
    # 月營收頁面混用 <td> 與 <Td>。
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        value = [cell(itor) for itor in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(value) >= 10 and value[0] == ticker:
            return value
    return None


def find_revenue(ticker, month, day):
    order = sorted(("sii", "otc"), key=lambda itor: itor != _market.get(ticker))
    for typek in order:
        for foreign in (False, True):
            url, html = revenue_page(typek, month, foreign, day)
            row = parse_revenue(html, ticker)
            if row:
                _market[ticker] = typek
                return typek, url, row
    return None, None, None


def revenue(ticker, months, day):
    """月營收：當月營收、去年同月與年增率。金額單位仟元。"""
    data = Dataset("revenue", f"{ticker} 月營收", ["月", "營收", "去年同月", "年增率"],
                   {"營收": "仟元", "去年同月": "仟元", "年增率": "%"})
    latest = previous_month(day)
    # 月營收於次月 10 日前公告
    if find_revenue(ticker, latest, day)[2] is None:
        data.missing.append(f"{latest:%Y-%m} 月營收尚未公告")
        latest = previous_month(latest)
    for month in recent_month(latest, months):
        typek, url, row = find_revenue(ticker, month, day)
        if row is None:
            data.missing.append(f"{month:%Y-%m} 月營收")
            continue
        data.add_source(f"公開資訊觀測站｜{MARKET_NAME[typek]}公司營業收入統計", f"{MOPS}/nas/t21/")
        data.row.append([f"{month:%Y-%m}", number(row[2]), number(row[4]), number(row[6])])
    return data


# ETF 淨值與持股

def yuanta(ticker):
    param = {"APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "PageName": f"/tradeInfo/pcf/{ticker}",
             "DeviceId": "null", "AppName": "ETF", "Device": 3, "Platform": "ETF",
             "FuncId": "PCF/Daily", "ticker": ticker}
    data = fetch_json(f"{YUANTA_API}?{urlencode(param)}")
    pcf = data.get("PCF") or {}
    if not pcf.get("nav"):
        raise ValueError(f"元大 ETF 資料查不到 {ticker}，目前只支援元大 ETF 與 {'、'.join(BLACKROCK)}")
    day = datetime.strptime(pcf["trandate"], "%Y%m%d").date()
    url = f"https://www.yuantaetfs.com/tradeInfo/pcf/{ticker}"
    nav = Dataset("nav", f"{ticker} 淨值與規模", ["日期", "淨值", "流通單位數", "淨資產"],
                  {"淨值": "元", "流通單位數": "單位", "淨資產": "元"})
    nav.add_source(f"元大投信｜{ticker} 申購買回清單", url, day)
    nav.row.append([day.isoformat(), pcf["nav"], pcf["osunit"], pcf["totalav"]])
    weight = data.get("FundWeights") or {}
    holding = Dataset("holding", f"{ticker} 持股", ["類別", "代號", "名稱", "權重", "數量"], {"權重": "%"},
                      "元大表列權重的分母為股票市值加期貨名目金額，不是基金淨資產")
    holding.add_source(f"元大投信｜{ticker} 基金持股", url, day)
    for kind, key, qty in (("股票", "StockWeights", "qty"), ("期貨", "FutureWeights", "qty"),
                           ("債券", "BondWeights", "FACE_AMT")):
        for itor in weight.get(key) or []:
            code = itor["code"] + (itor.get("ym") or "")
            holding.row.append([kind, code, itor["name"], number(itor["weights"]), number(itor.get(qty))])
    holding.row.sort(key=lambda itor: -(itor[3] or 0))
    return [nav, holding]


def csv_row(text):
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def blackrock(ticker, product):
    base = f"https://www.blackrock.com/tw/products/{product}/fund"
    url = f"{base}/1489751333078.ajax?fileType=csv&fileName=historical-nav&dataType=nav-component-table"
    row = [itor for itor in csv_row(fetch(url)) if itor]
    nav = Dataset("nav", f"{ticker} 淨值", ["日期", "淨值"], {"淨值": "元"})
    nav.add_source(f"貝萊德投信｜{ticker} 淨值明細", url)
    for itor in row[1:]:
        nav.row.append([roc_date(itor[0]).isoformat(), number(itor[-1])])
    nav.row.sort()
    url = f"{base}/1480664180144.ajax?dataType=fund&fileName={ticker}_holdings&fileType=csv"
    row = csv_row(fetch(url))
    day = roc_date(row[0][1])
    start = next(index for index, itor in enumerate(row) if itor and itor[0] == "Ticker")
    head = row[start]
    column = {name: head.index(name) for name in ("Ticker", "Name", "Sector", "Asset Class", "Weight (%)", "Location")}
    holding = Dataset("holding", f"{ticker} 持股", ["類別", "代號", "名稱", "權重", "產業", "地區"], {"權重": "%"},
                      "權重分母為基金淨資產")
    holding.add_source(f"貝萊德投信｜{ticker} 基金投資組合", url, day)
    for itor in row[start + 1:]:
        if len(itor) == len(head):
            holding.row.append([itor[column["Asset Class"]], itor[column["Ticker"]], itor[column["Name"]],
                                number(itor[column["Weight (%)"]]), itor[column["Sector"]], itor[column["Location"]]])
    holding.row.sort(key=lambda itor: -(itor[3] or 0))
    return [nav, holding]


def etf(ticker):
    return blackrock(ticker, BLACKROCK[ticker]) if ticker in BLACKROCK else yuanta(ticker)
