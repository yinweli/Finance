"""抓取籌碼、大盤與總經資料。

證交所與櫃買中心的籌碼表一次提供全市場一天的資料。過去日期的回應存入磁碟快取，
同一天研究多檔、或之後重跑時都不必重抓。
"""
import csv
import io
import json
from datetime import date, timedelta

from source import CACHE, Dataset, fetch, number, recent_month, roc_date, twse_rows


def past(day, today):
    return day < today


def ok_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return str(data.get("stat", "OK")).upper() == "OK" and bool(twse_rows(data)[1])


def market_json(url, day, today):
    """day 早於 today 時內容已固定，存入磁碟快取。"""
    text = fetch(url, keep=ok_json if past(day, today) else False)
    return json.loads(text) if text.strip().startswith("{") else {}


# 大盤

def month_end(month):
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def taiex(months, today):
    """加權指數收盤與大盤成交金額，也作為交易日曆。"""
    data = Dataset("taiex", "加權指數與成交金額", ["日期", "加權指數", "成交金額"], {"成交金額": "億元"})
    for month in recent_month(today, months):
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={month:%Y%m}01&response=json"
        row = twse_rows(market_json(url, month_end(month), today))[1]
        if not row:
            data.missing.append(f"{month:%Y-%m} 市場成交資訊")
            continue
        data.add_source("證交所｜市場成交資訊", url.split("?")[0])
        for itor in row:
            data.row.append([roc_date(itor[0]).isoformat(), number(itor[4]), round(number(itor[2]) / 1e8, 1)])
    return data


def otc_index(months, today):
    data = Dataset("otc_index", "櫃買指數", ["日期", "櫃買指數"])
    for month in recent_month(today, months):
        url = f"https://www.tpex.org.tw/www/zh-tw/indexInfo/inx?date={month:%Y/%m}/01&response=json"
        row = twse_rows(market_json(url, month_end(month), today))[1]
        if not row:
            data.missing.append(f"{month:%Y-%m} 櫃買指數")
            continue
        data.add_source("櫃買中心｜櫃買指數", url.split("?")[0])
        for itor in row:
            data.row.append([roc_date(itor[0]).isoformat(), number(itor[4])])
    return data


def trading_days(calendar, count):
    """從加權指數資料取最近 count 個交易日。"""
    return [date.fromisoformat(itor[0]) for itor in calendar.row][-count:]


# 全市場每日表

def twse_daily(kind, day, today):
    url = {
        "T86": f"https://www.twse.com.tw/rwd/zh/fund/T86?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json",
        "MARGN": f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={day:%Y%m%d}&selectType=ALL&response=json",
        "QFIIS": f"https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json",
        "TWT93U": f"https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?date={day:%Y%m%d}&response=json",
    }[kind]
    return url.split("?")[0], market_json(url, day, today)


def tpex_daily(kind, day, today):
    url = {
        "INST": f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={day:%Y/%m/%d}&response=json",
        "MARGIN": f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={day:%Y/%m/%d}&response=json",
        "QFII": f"https://www.tpex.org.tw/www/zh-tw/insti/qfii?date={day:%Y/%m/%d}&response=json",
    }[kind]
    return url.split("?")[0], market_json(url, day, today)


def find(row, ticker, index=0):
    return next((itor for itor in row if str(itor[index]).strip() == ticker), None)


def lots(value):
    value = number(value)
    return None if value is None else round(value / 1000)


def institutional(ticker, exchange, days, today):
    """三大法人買賣超（張）。"""
    data = Dataset("institutional", f"{ticker} 三大法人買賣超", ["日期", "外資", "投信", "自營商", "合計"],
                   {"外資": "張", "投信": "張", "自營商": "張", "合計": "張"}, "外資含外資自營商")
    for day in days:
        if exchange == "TWSE":
            url, raw = twse_daily("T86", day, today)
            row = find(twse_rows(raw)[1], ticker)
            # 欄位：外陸資買賣超(不含外資自營商)[4]、外資自營商買賣超[7]、投信買賣超[10]、自營商買賣超[11]、三大法人[-1]
            value = None if row is None else [
                lots(number(row[4]) + number(row[7])), lots(row[10]), lots(row[11]), lots(row[-1])]
            name = "證交所｜三大法人買賣超日報"
        else:
            url, raw = tpex_daily("INST", day, today)
            row = find(twse_rows(raw)[1], ticker)
            # 七組買進、賣出、買賣超：外資(不含自營)、外資自營、外資合計[10]、投信[13]、自營(自行)、自營(避險)、自營合計[22]，最後為三大法人合計
            value = None if row is None else [lots(row[10]), lots(row[13]), lots(row[22]), lots(row[23])]
            name = "櫃買中心｜三大法人買賣明細"
        if value is None:
            data.missing.append(f"{day} 三大法人")
            continue
        data.add_source(name, url)
        data.row.append([day.isoformat()] + value)
    return data


def margin(ticker, exchange, days, today):
    """個股融資、融券餘額（張）。"""
    data = Dataset("margin", f"{ticker} 融資融券餘額", ["日期", "融資餘額", "融券餘額"],
                   {"融資餘額": "張", "融券餘額": "張"})
    for day in days:
        if exchange == "TWSE":
            url, raw = twse_daily("MARGN", day, today)
            row = find(twse_rows(raw, 1)[1], ticker)
            value = None if row is None else [number(row[6]), number(row[12])]
            name = "證交所｜融資融券彙總"
        else:
            url, raw = tpex_daily("MARGIN", day, today)
            row = find(twse_rows(raw)[1], ticker)
            value = None if row is None else [number(row[6]), number(row[14])]
            name = "櫃買中心｜上櫃股票融資融券餘額"
        if value is None:
            data.missing.append(f"{day} 融資融券")
        else:
            data.add_source(name, url)
            data.row.append([day.isoformat()] + value)
    return data


def market_margin(days, today):
    data = Dataset("market_margin", "上市融資金額", ["日期", "融資金額"], {"融資金額": "億元"})
    for day in days:
        url, raw = twse_daily("MARGN", day, today)
        row = find(twse_rows(raw, 0)[1], "融資金額(仟元)")
        if row is None:
            data.missing.append(f"{day} 融資金額")
            continue
        data.add_source("證交所｜信用交易統計", url)
        data.row.append([day.isoformat(), round(number(row[5]) / 1e5, 1)])
    return data


def lending(ticker, days, today):
    """借券賣出餘額（張），只有上市股票。"""
    data = Dataset("lending", f"{ticker} 借券賣出餘額", ["日期", "借券賣出餘額"], {"借券賣出餘額": "張"})
    for day in days:
        url, raw = twse_daily("TWT93U", day, today)
        row = find(twse_rows(raw)[1], ticker)
        if row is None:
            data.missing.append(f"{day} 借券賣出餘額")
            continue
        data.add_source("證交所｜信用額度總量管制餘額表", url)
        data.row.append([day.isoformat(), lots(row[12])])
    return data


def foreign_ratio(ticker, exchange, calendar, months):
    """各月最後交易日的外資持股比率。"""
    data = Dataset("foreign_ratio", f"{ticker} 外資持股比率", ["日期", "外資持股比率"], {"外資持股比率": "%"})
    today = date.fromisoformat(calendar.row[-1][0]) if calendar.row else date.today()
    last = {}
    for itor in calendar.row:
        day = date.fromisoformat(itor[0])
        last[(day.year, day.month)] = day
    for day in sorted(last.values())[-months:]:
        if exchange == "TWSE":
            url, raw = twse_daily("QFIIS", day, today)
            row = find(twse_rows(raw)[1], ticker)
            value = None if row is None else number(row[7])
            name = "證交所｜外資及陸資投資持股統計"
        else:
            url, raw = tpex_daily("QFII", day, today)
            row = find(twse_rows(raw)[1], ticker, 1)
            value = None if row is None else number(row[7])
            name = "櫃買中心｜僑外資及陸資持股比例"
        if value is None:
            data.missing.append(f"{day} 外資持股比率")
            continue
        data.add_source(name, url)
        data.row.append([day.isoformat(), value])
    return data


# 集保股權分散

TDCC = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"


def holders(ticker):
    """集保股權分散表只提供最新一週，每次抓取時保存，逐週累積歷史。"""
    data = Dataset("holders", f"{ticker} 集保股權分散", ["日期", "400張以上", "1000張以上", "10張以下", "股東人數"],
                   {"400張以上": "%", "1000張以上": "%", "10張以下": "%", "股東人數": "人"},
                   "集保結算所只公開最新一週，歷史由每次研究累積")
    history = CACHE / "集保" / f"{ticker}.json"
    saved = json.loads(history.read_text(encoding="utf-8")) if history.exists() else {}
    text = fetch(TDCC)
    level = {}
    week = None
    for itor in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(itor) >= 6 and itor[1].strip() == ticker:
            week = itor[0]
            level[int(itor[2])] = (number(itor[3]), number(itor[5]))
    if level:
        data.add_source("集保結算所｜集保戶股權分散表", TDCC, week)
        saved[f"{week[:4]}-{week[4:6]}-{week[6:]}"] = [
            round(sum(level[i][1] for i in range(12, 16) if i in level), 2),
            level.get(15, (0, 0))[1],
            round(sum(level[i][1] for i in range(1, 4) if i in level), 2),
            level.get(17, (None, None))[0],
        ]
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    else:
        data.missing.append("最新一週股權分散")
    data.row = [[day] + value for day, value in sorted(saved.items())]
    return data


# 期貨與總經

def month_range(months, today):
    """逐月回傳（月初、月底或今天）。"""
    for month in recent_month(today, months):
        yield month, min(month_end(month), today)


def taifex_csv(url, form, keep):
    text = fetch(url, form, encoding="cp950", keep=(lambda t: t.startswith("日期")) if keep else False)
    return list(csv.reader(io.StringIO(text))) if text.startswith("日期") else []


def futures(months, today):
    """外資台指期多空未平倉口數淨額。"""
    data = Dataset("futures", "外資臺股期貨未平倉淨額", ["日期", "外資淨未平倉"], {"外資淨未平倉": "口"})
    url = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
    for month, end in month_range(months, today):
        row = taifex_csv(url, {"queryStartDate": f"{month:%Y/%m/%d}", "queryEndDate": f"{end:%Y/%m/%d}",
                               "commodityId": "TXF"}, end < today)
        if not row:
            data.missing.append(f"{month:%Y-%m} 期貨三大法人")
            continue
        data.add_source("期交所｜三大法人期貨未平倉", url)
        for itor in row[1:]:
            if len(itor) > 13 and "外資" in itor[2]:
                data.row.append([itor[0].replace("/", "-"), number(itor[13])])
    return data


def fx(months, today):
    data = Dataset("fx", "美元兌新臺幣", ["日期", "美元兌新臺幣"])
    url = "https://www.taifex.com.tw/cht/3/dailyFXRateDown"
    for month, end in month_range(months, today):
        row = taifex_csv(url, {"queryStartDate": f"{month:%Y/%m/%d}", "queryEndDate": f"{end:%Y/%m/%d}"}, end < today)
        if not row:
            data.missing.append(f"{month:%Y-%m} 匯率")
            continue
        data.add_source("期交所｜每日外幣參考匯率", url)
        for itor in row[1:]:
            if len(itor) > 1 and number(itor[1]) is not None:
                data.row.append([itor[0].replace("/", "-"), number(itor[1])])
    return data


def ust(years, today):
    """美國公債殖利率曲線（財政部每日公布）。"""
    data = Dataset("ust", "美國公債殖利率", ["日期", "2年", "10年", "20年", "30年"],
                   {"2年": "%", "10年": "%", "20年": "%", "30年": "%"})
    for year in range(today.year - years, today.year + 1):
        url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
        text = fetch(url, keep=(lambda t: t.startswith("Date")) if year < today.year else False)
        row = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        if not row or row[0][0] != "Date":
            data.missing.append(f"{year} 殖利率")
            continue
        data.add_source("美國財政部｜Daily Treasury Par Yield Curve Rates", url.split("?")[0])
        index = [row[0].index(name) for name in ("2 Yr", "10 Yr", "20 Yr", "30 Yr")]
        for itor in row[1:]:
            month, day, year_ = map(int, itor[0].split("/"))
            data.row.append([date(year_, month, day).isoformat()] + [number(itor[i]) for i in index])
    data.row.sort()
    start = today.replace(year=today.year - years).isoformat()
    data.row = [itor for itor in data.row if itor[0] >= start]
    return data
