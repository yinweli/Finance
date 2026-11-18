import json
import math
import re
from copy import deepcopy
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from calculations import calculate, change, total_return

MARKET = {"stock", "equity_etf", "bond_etf"}
EQUITY = {"stock", "equity_etf"}
ASSET = MARKET | {"spending", "retirement"}
KIND = {"verified", "guidance", "consensus", "assumption", "unverified"}
LEGACY_FIELD = {"subject", "as_of", "input", "assumption", "formula", "source"}
KNOWN_ASSET = {"0050": "equity_etf", "00713": "equity_etf", "009826": "equity_etf",
               "00679B": "bond_etf", "7584": "stock", "8936": "stock"}
PRIVATE_KEY = {
    "account_number", "account_id", "personal_holdings", "personal_budget",
    "raw_transactions", "raw_financial_records", "google_sheet_url",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_url(url):
    parsed = urlsplit(url)
    require(parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username
            and not re.search(r"[\s\\]", url), f"來源網址須為有效 http 或 https URL：{url}")
    require(not (parsed.hostname == "docs.google.com" and parsed.path.startswith("/spreadsheets/")),
            "研究快照不可包含個人 Google 試算表網址")
    return url


def unique_object(pair):
    result = {}
    for key, value in pair:
        require(key not in result, f"重複的 JSON 欄位 {key}")
        result[key] = value
    return result


def parse_json(text):
    def reject_constant(value):
        raise ValueError(f"無效的 JSON 數值 {value}")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    require(stamp.utcoffset() is not None, f"時間須含時區 {value}")
    return stamp


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def text_value(value):
    return isinstance(value, str) and bool(value.strip())


def subject_name(value):
    require(text_value(value) and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            and value == value.strip(" ."), "研究主題含無效檔名字元")
    return value


def source_refs(value, source):
    require(isinstance(value, list), "source 引用須為陣列")
    for key in value:
        require(isinstance(key, str) and key in source, f"找不到來源 {key}")
    return value


def record_list(value, field):
    require(isinstance(value, list) and all(isinstance(item, dict) for item in value),
            f"{field} 須為物件陣列")
    return value


def prepare(raw):
    data = deepcopy(raw)
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1,
            "請提供 schema_version 1 的研究資料")
    require(data.get("publishable", True) is True, "公開報告只接受可發布的去識別化資料")

    def check_private(item):
        if isinstance(item, dict):
            require(not (PRIVATE_KEY & item.keys()), "研究快照含個人財務欄位")
            for value in item.values():
                check_private(value)
        elif isinstance(item, list):
            for value in item:
                check_private(value)
        elif isinstance(item, str):
            require("docs.google.com/spreadsheets/" not in item.lower(), "研究快照含個人試算表網址")

    check_private(data)
    subject_name(data["subject"])
    require(date.fromisoformat(data["as_of"]).isoformat() == data["as_of"], "as_of 須為 YYYY-MM-DD")
    asset = data["asset_type"]
    require(asset in ASSET, f"未知研究類型 {asset}")
    cutoff = timestamp(data["data_cutoff"])
    ticker = data.get("ticker")
    if ticker in KNOWN_ASSET:
        require(asset == KNOWN_ASSET[ticker], f"{ticker} 的研究類型須為 {KNOWN_ASSET[ticker]}")
    if asset in MARKET:
        require(text_value(data["ticker"]) and text_value(data["exchange"]), "市場研究須有代號、交易所")
        require(timestamp(data["valuation_at"]) <= cutoff, "估值時點晚於資料截止時間")
    method = data["method"]
    mode = method["mode"]
    require(mode in ("plugin", "finance", "fallback"), f"未知研究方式 {mode}")
    if mode == "plugin":
        require(asset in EQUITY, f"{asset} 不適用 Public Equity Investing")
        require(text_value(method.get("skill")) and text_value(method.get("version")), "缺少官方 Skill 或版本")
        require(asset != "equity_etf" or method["skill"] not in
                {"initiating-coverage", "equity-model-update", "dcf-model-builder", "three-statement-model-builder"},
                "equity_etf 需使用 ETF／指數研究視角")
    else:
        require(text_value(method.get("detail")), "缺少 Finance 研究方式說明")

    source = data["source"]
    require(isinstance(source, dict) and source, "須提供來源表")
    for key, entry in source.items():
        require(text_value(entry["name"]), f"缺少來源名稱 {key}")
        if "url" in entry:
            safe_url(entry["url"])
        else:
            require(text_value(entry.get("locator")), f"缺少來源網址或文件位置 {key}")
        date.fromisoformat(entry["data_date"])
        if "published_at" in entry:
            published = entry["published_at"]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                require(timestamp(published) <= cutoff, f"來源 {key} 發布晚於資料截止")
            else:
                require(date.fromisoformat(published) <= cutoff.date(), f"來源 {key} 發布晚於資料截止")

    for field in ("input", "assumption", "formula"):
        require(isinstance(data[field], dict), f"{field} 須為物件")
    measure = data["input"] | data["assumption"] | data["formula"]
    require(len(measure) == sum(len(data[field]) for field in ("input", "assumption", "formula")),
            "輸入、假設及公式的 ID 不能重複")
    for key, entry in measure.items():
        require(re.fullmatch(r"[a-zA-Z0-9_-]+", key), f"無效的數值 ID {key}")
        require(text_value(entry["unit"]), f"缺少數值單位 {key}")
        precision = entry.get("precision", 2)
        require(type(precision) is int and 0 <= precision <= 10, f"顯示位數有誤 {key}")
        reference = source_refs(entry.get("source", []), source)
        if key in data["formula"]:
            require(isinstance(entry["args"], list) and all(
                isinstance(arg, str) or finite(arg) for arg in entry["args"]), f"公式參數錯誤 {key}")
            continue
        kind = entry["kind"]
        require(kind in KIND, f"未知證據分類 {kind}")
        require(date.fromisoformat(entry["date"]) <= cutoff.date(), f"數值 {key} 晚於資料截止")
        if "observed_at" in entry:
            require(timestamp(entry["observed_at"]) <= cutoff, f"數值 {key} 的時點晚於資料截止")
        value = entry["value"]
        require(value is None or finite(value), f"數值 {key} 須為有限數字或 null")
        if kind in {"verified", "guidance", "consensus"}:
            require(reference and value is not None, f"數值 {key} 缺少來源或數字")
        if kind in {"assumption", "unverified"} or value is None:
            require(text_value(entry.get("note")), f"數值 {key} 缺少假設依據或限制")
        if kind == "unverified":
            require(value is None, f"未核實數值 {key} 須保留缺口，可用另列的假設或代理")

    data["result"] = calculate(data)
    require(text_value(data["conclusion"]), "缺少一句結論")
    require(isinstance(data["limits"], list) and all(text_value(item) for item in data["limits"]), "限制須為文字陣列")
    require(isinstance(data["sections"], dict) and set(data["sections"]) <= {"data", "analysis", "scenarios", "review"},
            "報告區段有未知欄位")
    if asset in MARKET:
        valuation = data["valuation"]
        for key in ("fair", "as_of_price"):
            require(valuation[key] in measure, f"估值引用未知數值 {valuation[key]}")
        if "fair_high" in valuation:
            require(valuation["fair_high"] in measure, "估值區間引用未知數值")
        require(text_value(valuation["method"]) and text_value(valuation["cross_check"]), "缺少估值方法或交叉核對")
        require(text_value(data["thesis"]["view"]) and text_value(data["thesis"]["counter"]), "缺少論點或反證")
        require(set(data["scenarios"]) == {"bear", "base", "bull"}, "須提供保守、中性、樂觀三種情境")
        numeric = data["input"] | data["assumption"] | data["result"]
        price = numeric[valuation["as_of_price"]]
        fair = numeric[valuation["fair"]]
        if "fair_high" in valuation:
            high = numeric[valuation["fair_high"]]
            require(high["unit"] == fair["unit"] and (
                None in (fair["value"], high["value"]) or fair["value"] <= high["value"]
            ), "合理價區間或單位有誤")
        require(price["unit"] == fair["unit"], "合理價與市價單位不同")
        end_date = set()
        for label, case in data["scenarios"].items():
            require(all(case[key] in measure for key in ("price", "distribution", "return")), "情境引用未知數值")
            if "price_high" in case:
                require(case["price_high"] in measure and case["return_high"] in measure, "情境區間引用未知數值")
            require(date.fromisoformat(case["end_date"]) > date.fromisoformat(data["as_of"]), "情境期末不晚於研究日")
            end_date.add(case["end_date"])
            require(text_value(case["summary"]), "缺少情境假設說明")
            distribution = numeric[case["distribution"]]
            require(distribution["unit"] == price["unit"], "情境配息與市價單位不同")
            for suffix in ("", "_high") if "price_high" in case else ("",):
                terminal = numeric[case["price" + suffix]]
                outcome = numeric[case["return" + suffix]]
                require(terminal["unit"] == price["unit"] and outcome["unit"] == "%", "情境期末價或報酬單位有誤")
                amount = (price["value"], terminal["value"], distribution["value"])
                expected = total_return(*amount) if None not in amount else None
                actual = outcome["value"]
                require(actual == expected or (None not in (actual, expected) and
                        math.isclose(actual, expected, abs_tol=1e-8)), f"{label} 含息報酬與價格或配息不一致")
                key = f"case_{label}_price_change{suffix}"
                require(key not in measure, f"數值 ID {key} 保留給程式計算")
                source_id = set(price.get("source", [])) | set(terminal.get("source", []))
                data["result"][key] = {
                    "value": change(terminal["value"], price["value"]) if None not in amount[:2] else None,
                    "unit": "%", "source": sorted(source_id),
                }
            if "price_high" in case:
                low_value, high_value = (numeric[case[key]]["value"] for key in ("price", "price_high"))
                require(None in (low_value, high_value) or low_value <= high_value, "情境區間下限高於上限")
        require(len(end_date) == 1, "三種情境須使用同一期末日")
    require(isinstance(data["metrics"], dict) and data["metrics"], "缺少摘要指標")
    require(set(data["metrics"].values()) <= (measure | data["result"]).keys(), "摘要引用未知數值")
    for item in record_list(data.get("events", []), "events"):
        require(text_value(item["date"]) and text_value(item["title"]) and item["status"] in KIND, "事件資料不完整")
        reference = source_refs(item.get("source", []), source)
        require(reference or item["status"] in {"assumption", "unverified"}, "事件缺少來源")
    review = record_list(data["review"], "review")
    require(review and all(text_value(item["trigger"]) and text_value(item["action"]) for item in review),
            "缺少重估條件或應採動作")
    if "update" in data:
        update = data["update"]
        require(isinstance(update["changes"], list) and update["changes"]
                and all(text_value(item) for item in update["changes"]), "缺少更新差異")
        require(update["thesis"] in {"unchanged", "revised", "invalidated", "not_applicable"}
                and text_value(update["reason"]), "須交代前次論點狀態與原因")
    return data


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.snapshot = []
        self.active = False
        self.ids = set()
        self.anchors = set()
        self.remote = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        identifier = attr.get("id")
        if identifier:
            if identifier in self.ids:
                self.errors.append(f"重複 HTML id {identifier}")
            self.ids.add(identifier)
        if tag == "script" and identifier == "research-data":
            require(attr.get("type") == "application/json", "research-data 須為 application/json")
            self.active = True
            self.snapshot.append("")
        for key, value in attrs:
            if not value:
                continue
            if tag == "a" and key == "href":
                if value.startswith("#"):
                    self.anchors.add(value[1:])
                else:
                    safe_url(value)
            elif key in {"src", "srcset", "href", "xlink:href", "poster", "action", "data"}:
                if not value.startswith(("#", "data:")):
                    self.remote.append(value)

    def handle_data(self, data):
        if self.active:
            self.snapshot[-1] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


def snapshot(path):
    parser = ReportParser()
    parser.feed(path.read_text(encoding="utf-8-sig"))
    parser.close()
    require(len(parser.snapshot) == 1 and not parser.active, "須有一個完整的 research-data 快照")
    data = parse_json(parser.snapshot[0])
    require(isinstance(data, dict), "研究快照須為 JSON 物件")
    if "schema_version" not in data:
        require("subject" in data and "as_of" in data and bool((LEGACY_FIELD - {"subject", "as_of"}) & data.keys()),
                "舊版快照缺少研究資料欄位")
    else:
        require(type(data["schema_version"]) is int and data["schema_version"] == 1, "不支援此快照版本")
    return data


def merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
