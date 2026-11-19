"""解析與檢查 草稿.md／定稿.md，並計算情境報酬。格式見 流程/草稿格式.md。"""
import argparse
import json
import re
import sys
from pathlib import Path

SECTIONS = {
    "個股": ["關鍵數字", "結論", "財報與法說", "籌碼", "市場健康度", "估值", "情境與事件", "風險與重估", "限制", "來源"],
    "股票ETF": ["關鍵數字", "結論", "規則與成分股", "近 6 個月重大消息", "市場健康度", "估值", "情境與事件", "風險與重估",
              "限制", "來源"],
    "債券ETF": ["關鍵數字", "結論", "基金規則與持債", "利率與匯率", "近 6 個月重大消息", "估值", "情境與事件", "風險與重估",
              "限制", "來源"],
}
HORIZON = ("6個月", "12個月")
CASE = ("保守", "中性", "樂觀")
RATING = ("偏多", "中性", "偏空")


class DraftError(ValueError):
    pass


def parse(text):
    """回傳（標題, 關鍵數字 dict, [(章節標題, 內文)]）。"""
    title = re.search(r"^#\s+(.+)$", text, re.M)
    part = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)
    section = [(part[i].strip(), part[i + 1].strip("\n")) for i in range(1, len(part), 2)]
    key = None
    for name, body in section:
        if name == "關鍵數字":
            block = re.search(r"```json\s*\n(.*?)\n```", body, re.S)
            if not block:
                raise DraftError("「關鍵數字」章節沒有 ```json 區塊")
            try:
                key = json.loads(block.group(1))
            except ValueError as error:
                raise DraftError(f"關鍵數字 JSON 格式錯誤：{error}") from None
    if key is None:
        raise DraftError("找不到「## 關鍵數字」章節")
    return (title.group(1).strip() if title else ""), key, section


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compute(key):
    """依關鍵數字計算各情境漲跌幅、含息報酬，以及機率加權的期望價與期望報酬。"""
    price = key["現價"]["價格"]
    result = {}
    for horizon in HORIZON:
        case = key["情境"][horizon]
        cash = key.get("配息", {}).get(horizon, 0) or 0
        row = {}
        for name in CASE:
            target = case[name]["價格"]
            row[name] = {"價格": target, "機率": case[name]["機率"], "漲跌幅": (target / price - 1) * 100,
                         "含息報酬": ((target + cash) / price - 1) * 100}
        expect = sum(row[n]["價格"] * row[n]["機率"] for n in CASE) / 100
        row["期望"] = {"價格": expect, "漲跌幅": (expect / price - 1) * 100,
                     "含息報酬": ((expect + cash) / price - 1) * 100}
        result[horizon] = row
    low, high = key["合理區間"]["低"], key["合理區間"]["高"]
    result["合理區間位置"] = (price - low) / (high - low) * 100 if high > low else None
    result["合理中值折讓"] = ((low + high) / 2 / price - 1) * 100
    return result


def check_key(key, profile=None):
    problem = []

    def need(condition, message):
        if not condition:
            problem.append(message)
        return condition

    for name in ("代號", "名稱", "類型", "研究日", "現價", "合理區間", "情境", "配息", "評等", "事件", "重估條件", "資料缺口", "結論"):
        need(name in key, f"關鍵數字缺少「{name}」")
    if problem:
        return problem
    need(key["類型"] in SECTIONS, f"類型「{key['類型']}」不在 {list(SECTIONS)} 之中")
    if profile:
        need(str(key["代號"]) == profile["代號"], f"代號 {key['代號']} 與資料 {profile['代號']} 不符")
        need(key["類型"] == profile["類型"], f"類型 {key['類型']} 與標的文件 {profile['類型']} 不符")
        if "收盤" in profile:
            need(is_number(key["現價"].get("價格")) and abs(key["現價"]["價格"] - profile["收盤"]) < 0.005,
                 f"現價 {key['現價'].get('價格')} 與資料最新收盤 {profile['收盤']} 不符")
            need(key["現價"].get("日期") == profile["收盤日"],
                 f"現價日期 {key['現價'].get('日期')} 與資料最新收盤日 {profile['收盤日']} 不符")
    price = key["現價"].get("價格")
    need(is_number(price) and price > 0, "現價必須是正數")
    fair = key["合理區間"]
    if need(is_number(fair.get("低")) and is_number(fair.get("高")), "合理區間的低、高必須是數字"):
        need(0 < fair["低"] <= fair["高"], f"合理區間必須 0 < 低 ≤ 高，目前為 {fair['低']}～{fair['高']}")
    need(bool(fair.get("方法")), "合理區間缺少「方法」")
    for horizon in HORIZON:
        case = key["情境"].get(horizon)
        if not need(isinstance(case, dict) and all(n in case for n in CASE), f"情境「{horizon}」必須有保守、中性、樂觀"):
            continue
        value = [case[n].get("價格") for n in CASE]
        chance = [case[n].get("機率") for n in CASE]
        if need(all(is_number(v) and v > 0 for v in value), f"{horizon}情境價格必須是正數"):
            need(value[0] <= value[1] <= value[2], f"{horizon}情境價格必須保守 ≤ 中性 ≤ 樂觀，目前為 {value}")
        if need(all(isinstance(c, int) and 0 <= c <= 100 for c in chance), f"{horizon}情境機率必須是 0～100 的整數"):
            need(sum(chance) == 100, f"{horizon}情境機率合計必須為 100，目前為 {sum(chance)}")
        need(all(case[n].get("假設") for n in CASE), f"{horizon}每個情境都要寫「假設」")
    for horizon in HORIZON:
        cash = key["配息"].get(horizon)
        need(is_number(cash) and cash >= 0, f"配息「{horizon}」必須是 ≥ 0 的數字")
    for name in ("籌碼", "市場"):
        need(key["評等"].get(name) in RATING, f"評等「{name}」必須是 {'、'.join(RATING)} 之一")
    if key["類型"] == "個股":
        quarter = key.get("季預估")
        if need(isinstance(quarter, list) and len(quarter) == 8, "個股的「季預估」必須列出 8 季"):
            for itor in quarter:
                eps = itor.get("EPS") or {}
                ok = (re.fullmatch(r"\d{4}Q[1-4]", str(itor.get("季", ""))) and is_number(itor.get("營收"))
                      and is_number(itor.get("毛利率")) and all(is_number(eps.get(n)) for n in CASE))
                if need(ok, f"季預估 {itor.get('季')} 欄位不完整（季、營收、毛利率、EPS 三情境）"):
                    need(eps["保守"] <= eps["中性"] <= eps["樂觀"], f"季預估 {itor['季']} 的 EPS 必須保守 ≤ 中性 ≤ 樂觀")
    for itor in key["事件"]:
        label = itor.get("名稱", "（未命名）")
        need(all(itor.get(n) not in (None, "") for n in ("名稱", "日期", "狀態", "機率", "影響", "情境", "說明", "來源")),
             f"事件「{label}」欄位不完整")
        need(itor.get("狀態") in ("已發生", "預期"), f"事件「{label}」的狀態必須是已發生或預期")
        need(isinstance(itor.get("機率"), int) and 0 <= itor["機率"] <= 100, f"事件「{label}」的機率必須是 0～100 的整數")
        if itor.get("狀態") == "已發生":
            need(itor.get("機率") == 100, f"已發生的事件「{label}」機率必須為 100")
        need(is_number(itor.get("影響")), f"事件「{label}」的影響必須是數字（%）")
        need(itor.get("情境") in CASE, f"事件「{label}」的情境必須是保守、中性或樂觀")
    need(len(key["重估條件"]) >= 3, "重估條件至少 3 項")
    for itor in key["重估條件"]:
        need(all(itor.get(n) for n in ("指標", "門檻", "動作")), f"重估條件「{itor.get('指標')}」缺少指標、門檻或動作")
    need(isinstance(key["資料缺口"], list), "資料缺口必須是陣列")
    need(bool(str(key["結論"]).strip()), "結論不可空白")
    return problem


def check_text(text, profile=None):
    try:
        _, key, section = parse(text)
    except DraftError as error:
        return [str(error)], None
    problem = check_key(key, profile)
    expect = SECTIONS.get(key.get("類型"))
    if expect:
        actual = [name for name, _ in section]
        if actual != expect:
            problem.append(f"章節必須依序為：{'、'.join(expect)}；目前為：{'、'.join(actual)}")
    for name, body in section:
        if name != "關鍵數字" and re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", body):
            problem.append(f"章節「{name}」含有 HTML 標籤")
        if name not in ("關鍵數字",) and not body.strip():
            problem.append(f"章節「{name}」沒有內容")
    return problem, key


def check_file(path, work=None):
    path = Path(path)
    if not path.exists():
        return [f"找不到 {path}"], None
    work = Path(work) if work else path.parent
    meta = work / "資料" / "標的.json"
    profile = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None
    return check_text(path.read_text(encoding="utf-8"), profile)


def cli():
    parser = argparse.ArgumentParser(description="檢查草稿或定稿是否符合格式")
    parser.add_argument("path", type=Path)
    parser.add_argument("--work", type=Path, help="工作夾，預設為檔案所在資料夾")
    arg = parser.parse_args()
    problem, key = check_file(arg.path, arg.work)
    if problem:
        print("\n".join(f"- {itor}" for itor in problem))
        return 1
    result = compute(key)
    print(f"通過。現價 {key['現價']['價格']}，合理區間 {key['合理區間']['低']}～{key['合理區間']['高']}")
    for horizon in HORIZON:
        row = result[horizon]
        print(f"{horizon}：" + "、".join(f"{n} {row[n]['價格']:.2f}（{row[n]['含息報酬']:+.1f}%）" for n in CASE + ("期望",)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(cli())
