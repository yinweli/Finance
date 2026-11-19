"""公開資料連線、快取與共用解析。"""
import hashlib
import json
import re
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
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".tmp" / "快取"

_memory = {}
_last = {}


def today():
    return datetime.now(TZ).date()


def fetch(url, form=None, encoding="utf-8", keep=False):
    """取得網址內容。

    keep 表示內容不會再變（已過去的日期或月份），存入磁碟快取供下次重用。可傳入函式，
    回傳 True 才寫入快取，用來排除尚未公告或被拒絕查詢的回應。
    """
    key = url + "\n" + json.dumps(form, sort_keys=True)
    if key in _memory:
        return _memory[key]
    path = CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".txt")
    if keep and path.exists():
        _memory[key] = path.read_text(encoding="utf-8")
        return _memory[key]
    host = urlsplit(url).hostname
    body = urlencode(form).encode() if form else None
    # 尖峰時偶爾回 5xx 或連線逾時，間隔後重試；其他錯誤直接回報。
    for attempt in range(3):
        wait = _last.get(host, -PAUSE) + PAUSE * (1 + 3 * attempt) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            with urlopen(Request(url, data=body, headers={"User-Agent": AGENT}), timeout=60) as response:
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
    _memory[key] = text
    if keep and text.strip() and (keep is True or keep(text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def forget(url, form=None):
    """清除記憶體快取，讓下一次 fetch 重新連線。"""
    _memory.pop(url + "\n" + json.dumps(form, sort_keys=True), None)


def fetch_json(url, keep=False):
    text = fetch(url, keep=keep)
    return json.loads(text) if text.strip().startswith("{") else {}


def number(text):
    text = str(text).replace(",", "").replace("%", "").strip()
    if text in ("", "-", "--", "N/A", "None"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if re.fullmatch(r"-?\d+", text) else value


def roc_date(text):
    """解析民國或西元的年月日文字。"""
    year, month, day = map(int, re.findall(r"\d+", text)[:3])
    return date(year + 1911 if year < 1911 else year, month, day)


def cell(html):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", html))).replace("\xa0", "")


def month_start(day):
    return day.replace(day=1)


def previous_month(month):
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def recent_month(day, count):
    """回傳含 day 所在月份在內、往前 count 個月的月初，由舊到新。"""
    result = [month_start(day)]
    while len(result) < count:
        result.insert(0, previous_month(result[0]))
    return result


def quarter_end(year, season):
    return date(year, season * 3, 31 if season in (1, 4) else 30)


def previous_quarter(year, season):
    return (year, season - 1) if season > 1 else (year - 1, 4)


def recent_quarter(day, count):
    """回傳 day 之前已結束的最近 count 季，由舊到新。"""
    year, season = previous_quarter(day.year, (day.month - 1) // 3 + 1)
    result = []
    for _ in range(count):
        result.append((year, season))
        year, season = previous_quarter(year, season)
    return result[::-1]


def twse_rows(data, index=0):
    """證交所 rwd 介面：資料在 data，或在 tables[index].data。"""
    if str(data.get("stat", "OK")).upper() != "OK":
        return [], []
    if "tables" in data:
        table = data["tables"][index] if len(data["tables"]) > index else {}
        return table.get("fields") or [], table.get("data") or []
    return data.get("fields") or [], data.get("data") or []


class Dataset:
    """一份資料表：欄位、資料列、來源與缺漏。存成 資料/<name>.json。"""

    def __init__(self, name, title, column, unit=None, note=None):
        self.name, self.title, self.column = name, title, column
        self.unit, self.note = unit or {}, note
        self.row, self.source, self.missing = [], [], []

    def add_source(self, name, url, data_date=None):
        entry = {"名稱": name, "網址": url}
        if data_date:
            entry["資料日"] = str(data_date)
        if entry not in self.source:
            self.source.append(entry)

    def to_dict(self):
        data = {"名稱": self.title, "欄位": self.column, "單位": self.unit, "資料": self.row,
                "來源": self.source, "缺漏": self.missing}
        if self.note:
            data["附註"] = self.note
        return data

    def save(self, folder):
        path = Path(folder) / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return path
