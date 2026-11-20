"""把 定稿.md 與 資料/ 產生成單頁、可離線閱讀的 HTML 報告。"""
import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

from check import CASE, HORIZON, base_chance, check_file, compute, parse
from source import ROOT

REPORT = ROOT / "報告"
TEMPLATE = ROOT / "版型" / "報告.html"
COLOR = {"中性": "var(--s1)", "保守": "var(--s2)", "樂觀": "var(--s3)"}
DATA_NAME = "research-data"


def esc(text):
    return html.escape(str(text), quote=True)


def num(value, digit=2):
    if value is None:
        return "—"
    return f"{value:,.{digit}f}"


def signed(value, digit=1):
    """台灣慣例：上漲紅、下跌綠，並加上符號避免只靠顏色。"""
    if value is None:
        return "—"
    mark, cls = ("▲", "up") if value > 0 else ("▼", "down") if value < 0 else ("", "flat")
    return f'<span class="{cls}">{mark}{abs(value):.{digit}f}%</span>'


# Markdown（只支援報告需要的語法）

def inline(text):
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"(?<![\"'>])(https?://[^\s<]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
    return text


def markdown(text):
    out, para, lines = [], [], text.strip("\n").split("\n")

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush()
            i += 1
        elif line.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(block)) + "</code></pre>")
            i += 1
        elif re.match(r"^#{3,4}\s", line):
            flush()
            level = 3 if line.startswith("### ") else 4
            out.append(f"<h{level}>{inline(line.lstrip('#').strip())}</h{level}>")
            i += 1
        elif line.lstrip().startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in block[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
            out.append(table_html(block[0], body, raw=False))
        elif re.match(r"^\s*([-*]|\d+\.)\s", line):
            flush()
            tag = "ol" if re.match(r"^\s*\d+\.", line) else "ul"
            item = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s", lines[i]):
                item.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]))
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                    item[-1] += " " + lines[i].strip()
                    i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{inline(t)}</li>" for t in item) + f"</{tag}>")
        elif line.startswith(">"):
            flush()
            out.append(f'<p class="note">{inline(line.lstrip("> "))}</p>')
            i += 1
        else:
            para.append(line.strip())
            i += 1
    flush()
    return "\n".join(out)


def table_html(head, body, raw=True, cls=""):
    cell = (lambda v: str(v)) if raw else inline
    line = [f'<div class="table-wrap"><table class="{cls}"><thead><tr>']
    line += [f"<th>{cell(h)}</th>" for h in head]
    line.append("</tr></thead><tbody>")
    for row in body:
        line.append("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in row) + "</tr>")
    line.append("</tbody></table></div>")
    return "".join(line)


# 圖表（內嵌 SVG）

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 260, 56, 16, 16, 28


def nice(low, high, count=5):
    if low == high:
        low, high = low - 1, high + 1
    span = high - low
    step = 10 ** int(f"{span / count:e}".split("e")[1])
    for mult in (1, 2, 2.5, 5, 10):
        if span / (step * mult) <= count:
            step *= mult
            break
    start = (low // step) * step
    tick = []
    value = start
    while value <= high + step * 0.001:
        tick.append(round(value, 10))
        value += step
    if tick[-1] < high:
        tick.append(tick[-1] + step)
    return tick


def thin(label, limit=8):
    if len(label) <= limit:
        return list(range(len(label)))
    step = max(1, round(len(label) / limit))
    index = list(range(0, len(label), step))
    if index[-1] != len(label) - 1:
        index[-1] = len(label) - 1
    return index


def frame(tick, y, label, x, unit=""):
    """格線、Y 軸刻度與 X 軸標籤。"""
    part = []
    for value in tick:
        py = y(value)
        part.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{py:.1f}" y2="{py:.1f}"/>')
        part.append(f'<text class="axis" x="{PAD_L - 6}" y="{py + 4:.1f}" text-anchor="end">{axis_num(value)}</text>')
    for index in thin(label):
        part.append(f'<text class="axis" x="{x(index):.1f}" y="{H - 8}" text-anchor="middle">{esc(label[index])}</text>')
    return part


def axis_num(value):
    if abs(value) >= 10000:
        return f"{value / 10000:,.1f}萬"
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def svg(part, title, legend=None, height=H):
    head = f'<figure class="chart"><figcaption>{esc(title)}</figcaption>'
    if legend:
        head += '<div class="legend">' + "".join(
            f'<span><i style="background:{color}{";height:2px" if kind == "line" else ""}"></i>{esc(name)}</span>'
            for name, color, kind in legend) + "</div>"
    return (head + f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}">' + "".join(part)
            + "</svg></figure>")


def line_chart(title, label, series, unit="", band=None, mark=None):
    """series：[(名稱, 顏色, 數值陣列, 樣式)]，樣式為 solid 或 dash。數值可為 None。"""
    value = [v for _, _, data, _ in series for v in data if v is not None]
    if band:
        value += [band[0], band[1]]
    if not value:
        return ""
    tick = nice(min(value), max(value))
    low, high = tick[0], tick[-1]
    n = max(len(label) - 1, 1)
    x = lambda i: PAD_L + (W - PAD_L - PAD_R) * i / n
    y = lambda v: PAD_T + (H - PAD_T - PAD_B) * (1 - (v - low) / (high - low))
    part = frame(tick, y, label, x, unit)
    if band:
        part.append(f'<rect class="band" x="{PAD_L}" width="{W - PAD_L - PAD_R}" y="{y(band[1]):.1f}" '
                    f'height="{y(band[0]) - y(band[1]):.1f}"/>')
        part.append(f'<text class="band-label" x="{PAD_L + 6}" y="{y(band[1]) + 12:.1f}">{esc(band[2])}</text>')
    for name, color, data, style in series:
        path, pen = [], False
        for i, v in enumerate(data):
            if v is None:
                pen = False
                continue
            path.append(f"{'L' if pen else 'M'}{x(i):.1f},{y(v):.1f}")
            pen = True
        dash = ' stroke-dasharray="6 4"' if style == "dash" else ""
        part.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2"{dash}/>')
    if mark is not None:
        part.append(f'<line class="divider" x1="{x(mark):.1f}" x2="{x(mark):.1f}" y1="{PAD_T}" y2="{H - PAD_B}"/>')
    # 每個 X 位置一個透明熱區，滑過時顯示該點所有數值
    step = (W - PAD_L - PAD_R) / n
    for i, text in enumerate(label):
        tip = [esc(text)] + [f"{esc(name)}：{num(data[i])}" for name, _, data, _ in series if i < len(data) and data[i] is not None]
        if len(tip) > 1:
            part.append(f'<rect class="hit" x="{x(i) - step / 2:.1f}" y="{PAD_T}" width="{max(step, 2):.1f}" '
                        f'height="{H - PAD_T - PAD_B}" data-tip="{"&#10;".join(tip)}" data-x="{x(i):.1f}"/>')
    legend = [(name, color, "line") for name, color, _, _ in series] if len(series) > 1 else None
    return svg(part, title, legend)


def bar_chart(title, label, value, unit="", color=None, forecast_from=None, whisker=None, signed_color=False):
    """單一系列長條圖。forecast_from 之後為預估（淡色）；whisker 為 [(低, 高)] 區間。"""
    data = [v for v in value if v is not None] + [0]
    if whisker:
        data += [w for pair in whisker if pair for w in pair if w is not None]
    if len(data) == 1:
        return ""
    tick = nice(min(data), max(data))
    low, high = tick[0], tick[-1]
    n = len(label)
    slot = (W - PAD_L - PAD_R) / n
    x = lambda i: PAD_L + slot * (i + 0.5)
    y = lambda v: PAD_T + (H - PAD_T - PAD_B) * (1 - (v - low) / (high - low))
    part = frame(tick, y, label, x, unit)
    width = max(min(slot - 2, 28), 2)
    for i, v in enumerate(value):
        if v is None:
            continue
        fill = ("var(--up)" if v >= 0 else "var(--down)") if signed_color else (color or "var(--s1)")
        top, bottom = (y(v), y(0)) if v >= 0 else (y(0), y(v))
        future = forecast_from is not None and i >= forecast_from
        part.append(f'<rect x="{x(i) - width / 2:.1f}" y="{top:.1f}" width="{width:.1f}" height="{max(bottom - top, 1):.1f}" '
                    f'rx="2" fill="{fill}"{" class=\"forecast\"" if future else ""}/>')
        tip = f"{esc(label[i])}{'（預估）' if future else ''}&#10;{num(v)} {esc(unit)}"
        if whisker and i < len(whisker) and whisker[i]:
            lo, hi = whisker[i]
            part.append(f'<line class="whisker" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{y(lo):.1f}" y2="{y(hi):.1f}"/>')
            tip += f"&#10;保守～樂觀：{num(lo)}～{num(hi)}"
        part.append(f'<rect class="hit" x="{x(i) - slot / 2:.1f}" y="{PAD_T}" width="{slot:.1f}" height="{H - PAD_T - PAD_B}" '
                    f'data-tip="{tip}" data-x="{x(i):.1f}"/>')
    if 0 > low:
        part.append(f'<line class="zero" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    if forecast_from is not None and 0 < forecast_from < n:
        sep = PAD_L + slot * forecast_from
        part.append(f'<line class="divider" x1="{sep:.1f}" x2="{sep:.1f}" y1="{PAD_T}" y2="{H - PAD_B}"/>')
        part.append(f'<text class="axis" x="{sep + 4:.1f}" y="{PAD_T + 10}">預估 →</text>')
    return svg(part, title)


def hbar_chart(title, item, unit="%"):
    """水平長條：[(名稱, 數值)]，依數值排序。"""
    if not item:
        return ""
    row = 26
    height = row * len(item) + 12
    top = max(v for _, v in item) or 1
    short = [n if len(n) <= 22 else n[:20] + "…" for n, _ in item]
    # 中文字約佔兩個英文字寬，依最長名稱決定左側寬度
    width = max(sum(2 if ord(c) > 0x2E80 else 1 for c in n) for n in short)
    left, right = min(240, 24 + width * 7), 70
    part = []
    for i, (name, value) in enumerate(item):
        cy = 6 + i * row
        length = (W - left - right) * value / top
        part.append(f'<text class="axis label" x="{left - 8}" y="{cy + 15}" text-anchor="end">{esc(short[i])}</text>')
        part.append(f'<rect x="{left}" y="{cy + 4}" width="{max(length, 2):.1f}" height="16" rx="2" fill="var(--s1)"/>')
        part.append(f'<text class="value" x="{left + length + 6:.1f}" y="{cy + 16}">{num(value)}{esc(unit)}</text>')
        part.append(f'<rect class="hit" x="0" y="{cy}" width="{W}" height="{row}" data-tip="{esc(name)}&#10;{num(value)}{esc(unit)}"/>')
    return svg(part, title, height=height)


def range_chart(key, result):
    """點圖：合理區間、6 個月與 12 個月三情境，現價以貫穿各列的虛線表示。"""
    fair = key["合理區間"]
    price = key["現價"]["價格"]
    value = [fair["低"], fair["高"], price] + [key["情境"][h][n]["價格"] for h in HORIZON for n in CASE]
    tick = nice(min(value), max(value), 6)
    low, high = tick[0], tick[-1]
    left, right, top, row = 120, 24, 34, 52
    height = top + row * 3 + 26
    x = lambda v: left + (W - left - right) * (v - low) / (high - low)
    part = []
    for t in tick:
        part.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top - 6}" y2="{top + row * 3 - 10}"/>')
        part.append(f'<text class="axis" x="{x(t):.1f}" y="{height - 6}" text-anchor="middle">{axis_num(t)}</text>')
    # 第 1 列：合理區間
    cy = top + row * 0.5 - 4
    part.append(f'<text class="axis label" x="{left - 12}" y="{cy + 4}" text-anchor="end">合理區間</text>')
    part.append(f'<rect class="fair" x="{x(fair["低"]):.1f}" y="{cy - 10}" width="{max(x(fair["高"]) - x(fair["低"]), 2):.1f}" '
                f'height="20" rx="4" data-tip="合理區間&#10;{num(fair["低"])}～{num(fair["高"])} 元"/>')
    part.append(f'<text class="value" x="{x(fair["低"]) - 6:.1f}" y="{cy + 4}" text-anchor="end">{num(fair["低"])}</text>')
    part.append(f'<text class="value" x="{x(fair["高"]) + 6:.1f}" y="{cy + 4}">{num(fair["高"])}</text>')
    # 第 2、3 列：情境
    for i, horizon in enumerate(HORIZON, 1):
        cy = top + row * (i + 0.5) - 4
        case = key["情境"][horizon]
        expect = result[horizon]["期望"]["價格"]
        part.append(f'<text class="axis label" x="{left - 12}" y="{cy + 4}" text-anchor="end">{horizon}情境</text>')
        part.append(f'<line class="whisker" x1="{x(case["保守"]["價格"]):.1f}" x2="{x(case["樂觀"]["價格"]):.1f}" '
                    f'y1="{cy}" y2="{cy}"/>')
        for name in CASE:
            v = case[name]["價格"]
            part.append(f'<circle cx="{x(v):.1f}" cy="{cy}" r="7" fill="{COLOR[name]}" stroke="var(--surface)" stroke-width="2" '
                        f'data-tip="{horizon}{name}（機率 {case[name]["機率"]}%）&#10;{num(v)} 元"/>')
        for name in ("保守", "樂觀"):
            v = case[name]["價格"]
            part.append(f'<text class="value" x="{x(v):.1f}" y="{cy + 22}" text-anchor="middle">{num(v)}</text>')
        ex = x(expect)
        part.append(f'<path d="M{ex:.1f},{cy - 13} l6,6 l-6,6 l-6,-6 z" fill="var(--ink)" stroke="var(--surface)" stroke-width="1.5" '
                    f'data-tip="{horizon}機率加權期望價&#10;{num(expect)} 元"/>')
    # 現價
    px = x(price)
    part.append(f'<line class="divider" x1="{px:.1f}" x2="{px:.1f}" y1="{top - 10}" y2="{top + row * 3 - 10}"/>')
    part.append(f'<text class="axis label" x="{px:.1f}" y="{top - 16}" text-anchor="middle">現價 {num(price)}</text>')
    legend = [(n, COLOR[n], "dot") for n in CASE] + [("機率加權期望價（◆）", "var(--ink)", "dot")]
    return svg(part, "合理區間與 6／12 個月情境（元）", legend, height=height)


def scenario_chart(key, result, data):
    """近 1 年收盤，接上 6、12 個月後三情境的價格路徑。"""
    row = data.get("price", {}).get("資料", [])[-250:]
    if not row:
        return ""
    weekly = row[::5] if row[-1] == row[::5][-1] else row[::5] + [row[-1]]
    label = [r[0][2:7].replace("-", "/") for r in weekly]
    close = [r[4] for r in weekly]
    last = len(close) - 1
    future = ["+3月", "+6月", "+9月", "+12月"]
    label += future
    series = [("收盤", "var(--ink2)", close + [None] * 4, "solid")]
    price = key["現價"]["價格"]
    for name in ("樂觀", "中性", "保守"):
        six = key["情境"]["6個月"][name]["價格"]
        twelve = key["情境"]["12個月"][name]["價格"]
        path = [None] * last + [price, (price + six) / 2, six, (six + twelve) / 2, twelve]
        series.append((name, COLOR[name], path, "dash"))
    fair = key["合理區間"]
    return line_chart("股價與 6／12 個月情境（元）", label, series, band=(fair["低"], fair["高"], "合理區間"), mark=last)


def chart_for(name, key, data):
    """依章節名稱插入對應圖表。"""
    part = []
    get = lambda n: data.get(n, {}).get("資料", [])
    if name == "財報與法說":
        row = get("financial")
        quarter = key.get("季預估") or []
        label = [r[0] for r in row] + [q["季"] for q in quarter]
        revenue = [r[1] / 1e5 if r[1] is not None else None for r in row] + [q["營收"] for q in quarter]
        part.append(bar_chart("單季營收（億元）", label, revenue, "億元", forecast_from=len(row) if quarter else None))
        eps = [r[6] for r in row] + [q["EPS"]["中性"] for q in quarter]
        whisker = [None] * len(row) + [(q["EPS"]["保守"], q["EPS"]["樂觀"]) for q in quarter]
        part.append(bar_chart("單季 EPS（元，預估為中性，線段為保守～樂觀）", label, eps, "元",
                              forecast_from=len(row) if quarter else None, whisker=whisker, color="var(--s1)"))
        month = get("revenue")[-24:]
        if month:
            part.append(bar_chart("月營收（億元）", [r[0][2:].replace("-", "/") for r in month],
                                  [r[1] / 1e5 if r[1] else None for r in month], "億元", color="var(--s1)"))
    elif name == "籌碼":
        row = get("institutional")
        if row:
            part.append(bar_chart("三大法人每日買賣超合計（張）", [r[0][5:].replace("-", "/") for r in row], [r[4] for r in row],
                                  "張", signed_color=True))
            total, run = [], {1: 0, 2: 0, 3: 0}
            for r in row:
                for i in run:
                    run[i] += r[i] or 0
                total.append(dict(run))
            part.append(line_chart("三大法人累計買賣超（張）", [r[0][5:].replace("-", "/") for r in row],
                                   [(n, c, [t[i] for t in total], "solid") for i, n, c in
                                    ((1, "外資", "var(--s1)"), (2, "投信", "var(--s2)"), (3, "自營商", "var(--s3)"))]))
        row = get("margin")
        if row:
            part.append(line_chart("融資餘額（張）", [r[0][5:].replace("-", "/") for r in row],
                                   [("融資餘額", "var(--s1)", [r[1] for r in row], "solid")]))
        row = get("foreign_ratio")
        if row:
            part.append(line_chart("外資持股比率（%）", [r[0][2:7].replace("-", "/") for r in row],
                                   [("外資持股比率", "var(--s1)", [r[1] for r in row], "solid")]))
        row = get("holders")
        if len(row) > 1:
            part.append(line_chart("集保 400 張以上大戶持股比例（%）", [r[0][5:].replace("-", "/") for r in row],
                                   [("400 張以上", "var(--s1)", [r[1] for r in row], "solid")]))
    elif name == "市場健康度":
        row = get("taiex")
        if row:
            index = [r[1] for r in row]
            ma = lambda n: [sum(index[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(index))]
            pick = list(range(0, len(row), 5))
            if pick[-1] != len(row) - 1:
                pick.append(len(row) - 1)
            m60, m240 = ma(60), ma(240)
            part.append(line_chart("加權指數與均線", [row[i][0][2:7].replace("-", "/") for i in pick],
                                   [("加權指數", "var(--ink2)", [index[i] for i in pick], "solid"),
                                    ("60 日均線", "var(--s1)", [m60[i] for i in pick], "solid"),
                                    ("240 日均線", "var(--s2)", [m240[i] for i in pick], "solid")]))
        row = get("market_margin")
        if row:
            part.append(line_chart("上市融資金額（億元）", [r[0][5:].replace("-", "/") for r in row],
                                   [("融資金額", "var(--s1)", [r[1] for r in row], "solid")]))
        row = get("futures")
        if row:
            part.append(bar_chart("外資台指期淨未平倉（口）", [r[0][5:].replace("-", "/") for r in row], [r[1] for r in row],
                                  "口", signed_color=True))
    elif name in ("規則與成分股", "基金規則與持債"):
        row = [r for r in get("holding") if r[3] is not None][:10]
        if row:
            part.append(hbar_chart("前十大持股權重", [(f"{r[2]}", r[3]) for r in row]))
        row = get("scale")
        if len(row) > 1:
            part.append(line_chart("流通單位數", [r[0][5:] for r in row], [("流通單位數", "var(--s1)", [r[2] for r in row], "solid")]))
    elif name == "利率與匯率":
        row = get("ust")
        if row:
            pick = row[::5] + ([row[-1]] if (len(row) - 1) % 5 else [])
            part.append(line_chart("美國公債殖利率（%）", [r[0][2:7].replace("-", "/") for r in pick],
                                   [("20 年", "var(--s1)", [r[3] for r in pick], "solid"),
                                    ("30 年", "var(--s2)", [r[4] for r in pick], "solid")]))
    if name in ("利率與匯率", "規則與成分股") and key.get("類型") != "個股":
        row = get("fx")
        if row and (name == "利率與匯率" or key.get("代號") == "009826"):
            pick = row[::5] + ([row[-1]] if (len(row) - 1) % 5 else [])
            part.append(line_chart("美元兌新臺幣", [r[0][2:7].replace("-", "/") for r in pick],
                                   [("匯率", "var(--s1)", [r[1] for r in pick], "solid")]))
    if name == "估值":
        row = get("valuation")
        if row:
            pick = row[::5] + ([row[-1]] if (len(row) - 1) % 5 else [])
            part.append(line_chart("本益比（倍，交易所公布）", [r[0][2:7].replace("-", "/") for r in pick],
                                   [("本益比", "var(--s1)", [r[1] for r in pick], "solid")]))
    return "".join(itor for itor in part if itor)


# 結構化區塊

def summary_card(key, result):
    price = key["現價"]
    fair = key["合理區間"]
    tile = [("現價", f"{num(price['價格'])}", f"{price['日期']} 收盤"),
            ("合理區間", f"{num(fair['低'])}～{num(fair['高'])}", f"中值相對現價 {signed(result['合理中值折讓'])}")]
    for horizon in HORIZON:
        row = result[horizon]["期望"]
        tile.append((f"{horizon}期望價", num(row["價格"]), f"含息報酬 {signed(row['含息報酬'])}"))
    rating = "".join(f'<span class="pill">{esc(k)}：{esc(v)}</span>' for k, v in key["評等"].items())
    return ('<section class="summary" id="sec-summary">'
            f'<p class="verdict">{esc(key["結論"])}</p>'
            '<div class="tiles">' + "".join(
                f'<div class="tile"><div class="tile-label">{esc(a)}</div><div class="tile-value">{b}</div>'
                f'<div class="tile-note">{c}</div></div>' for a, b, c in tile) + "</div>"
            f'<div class="pills">{rating}</div>' + range_chart(key, result) + "</section>")


def scenario_table(key, result, base=None):
    """base 是依歷史波動推算的基準機率，有資料時多列一欄，方便看出研究判斷偏離多少。"""
    head = ["期間", "情境", "機率"] + (["歷史基準"] if base else []) + ["期末價", "漲跌幅", "含息報酬", "主要假設"]
    body = []
    for horizon in HORIZON:
        for name in CASE:
            row = result[horizon][name]
            body.append([horizon, f'<span class="dot" style="background:{COLOR[name]}"></span>{name}', f"{row['機率']}%"]
                        + ([f"{base[horizon][name]:.0f}%"] if base else [])
                        + [num(row["價格"]), signed(row["漲跌幅"]), signed(row["含息報酬"]), esc(key["情境"][horizon][name]["假設"])])
        row = result[horizon]["期望"]
        body.append([horizon, "<strong>機率加權</strong>", "100%"] + (["100%"] if base else [])
                    + [f"<strong>{num(row['價格'])}</strong>", signed(row["漲跌幅"]), signed(row["含息報酬"]),
                       f"期間預估配息 {num(key['配息'][horizon])} 元"])
    note = ""
    if base:
        note = '<p class="note">歷史基準：用近 2 年股價波動度、假設不漲不跌推算的機率，代表「只看波動、不做判斷」時的分布。'
        if key.get("機率說明"):
            note += f"研究判斷和基準不同的理由：{esc(key['機率說明'])}"
        note += "</p>"
    return table_html(head, body, cls="scenario") + note


def event_table(key):
    if not key["事件"]:
        return ""
    # 欄位多、說明長，用表格會擠成橫向捲動，所以每個事件排成一張卡片
    card = []
    for e in sorted(key["事件"], key=lambda e: str(e["日期"])):
        state = "pending" if e["狀態"] == "預期" else "done"
        card.append(
            f'<li class="event {state}"><div class="event-head"><span class="event-date">{esc(e["日期"])}</span>'
            f'<strong class="event-name">{esc(e["名稱"])}</strong></div>'
            f'<div class="event-meta"><span class="tag">{esc(e["狀態"])}</span><span>機率 {e["機率"]}%</span>'
            f'<span>對合理價影響 {signed(e["影響"])}</span>'
            f'<span><span class="dot" style="background:{COLOR[e["情境"]]}"></span>{esc(e["情境"])}情境</span></div>'
            f'<p>{inline(e["說明"])}</p><div class="event-source">來源：{inline(e["來源"])}</div></li>')
    expect = sum(e["機率"] * e["影響"] for e in key["事件"] if e["狀態"] == "預期") / 100
    note = f'<p class="note">尚未發生事件的機率加權影響合計：{signed(expect)}（僅供參考，事件之間可能重疊）。</p>'
    return '<h3>事件影響</h3><ul class="events">' + "".join(card) + "</ul>" + note


def review_table(key):
    body = [[esc(r["指標"]), esc(r["門檻"]), esc(r["動作"])] for r in key["重估條件"]]
    return "<h3>重估條件</h3>" + table_html(["指標", "門檻", "觸發後動作"], body)


def gap_list(key):
    if not key["資料缺口"]:
        return ""
    return ("<h3>資料缺口</h3><ul>" + "".join(f"<li><strong>{esc(g.get('項目', ''))}</strong>：{esc(g.get('影響', ''))}</li>"
                                             for g in key["資料缺口"]) + "</ul>")


def data_source(data):
    seen, line = set(), []
    for name, item in data.items():
        for src in item.get("來源", []):
            label = (src["名稱"], src["網址"])
            if label in seen:
                continue
            seen.add(label)
            line.append(f'<li>{esc(src["名稱"])}：<a href="{esc(src["網址"])}" target="_blank" rel="noopener">{esc(src["網址"])}</a></li>')
    return "<h3>程式抓取的資料</h3><ul class=\"sources\">" + "".join(line) + "</ul>" if line else ""


# 組裝

def load_data(work):
    folder = Path(work) / "資料"
    data = {}
    for path in sorted(folder.glob("*.json")):
        if path.stem != "標的":
            data[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return data


def build(text, work):
    title, key, section = parse(text)
    result = compute(key)
    data = load_data(work)
    meta_path, setting_path = Path(work) / "資料" / "標的.json", Path(work) / "執行設定.json"
    base = base_chance(key, json.loads(meta_path.read_text(encoding="utf-8"))) if meta_path.exists() else None
    setting = json.loads(setting_path.read_text(encoding="utf-8")) if setting_path.exists() else {}
    nav, body = [], []
    for name, content in section:
        if name == "關鍵數字":
            continue
        anchor = f"sec-{len(nav)}"
        nav.append(f'<a href="#{anchor}">{esc(name)}</a>')
        extra_before, extra_after = "", ""
        if name == "結論":
            extra_before = summary_card(key, result)
        elif name == "情境與事件":
            extra_before = scenario_chart(key, result, data) + scenario_table(key, result, base) + event_table(key)
        elif name == "風險與重估":
            extra_before = review_table(key)
        elif name == "限制":
            extra_before = gap_list(key)
        elif name == "來源":
            extra_after = data_source(data) + (
                '<p class="note">執行設定：' + "；".join(f"{esc(k)} {esc(v)}" for k, v in setting.items()) + "</p>"
                if setting else "")
        chart = chart_for(name, key, data)
        body.append(f'<section id="{anchor}"><h2>{esc(name)}</h2>{extra_before}{markdown(content)}'
                    f'{"<div class=\"charts\">" + chart + "</div>" if chart else ""}{extra_after}</section>')
    meta = {"關鍵數字": key, "計算": result, "產生時間": date.today().isoformat()}
    if base:
        meta["歷史基準機率"] = {h: {n: round(v, 1) for n, v in row.items()} for h, row in base.items()}
    if setting:
        meta["執行設定"] = setting
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    return (template.replace("{{title}}", esc(title or f"{key['代號']} {key['名稱']}"))
            .replace("{{subtitle}}", esc(f"{key['類型']}｜研究日 {key['研究日']}｜現價日期 {key['現價']['日期']}"))
            .replace("{{nav}}", "".join(nav))
            .replace("{{body}}", "\n".join(body))
            .replace("{{data}}", f'<script type="application/json" id="{DATA_NAME}">{payload}</script>'))


def output_path(key, folder=None):
    base = Path(folder or REPORT) / str(key["代號"])
    path = base / f"{key['研究日']}.html"
    version = 2
    while path.exists():
        path = base / f"{key['研究日']}-v{version}.html"
        version += 1
    return path


def reports(ticker, folder=None):
    """回傳該標的所有報告，依研究日與版本由舊到新排序。"""
    base = Path(folder or REPORT) / ticker

    def order(path):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:-v(\d+))?", path.stem)
        return (match.group(1), int(match.group(2) or 1)) if match else ("", 0)

    return sorted(base.glob("*.html"), key=order) if base.exists() else []


def latest(ticker, folder=None):
    """回傳該標的最新一份報告的路徑與內嵌資料，沒有時回傳 (None, None)。"""
    report = reports(ticker, folder)
    if not report:
        return None, None
    return report[-1], extract(report[-1])


def extract(path):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(rf'<script type="application/json" id="{DATA_NAME}">(.*?)</script>', text, re.S)
    return json.loads(match.group(1).replace("<\\/", "</")) if match else None


def render(path, work=None, out=None):
    path = Path(path)
    work = Path(work) if work else path.parent
    problem, key = check_file(path, work)
    if problem:
        raise ValueError("定稿未通過檢查：\n" + "\n".join(f"- {p}" for p in problem))
    page = build(path.read_text(encoding="utf-8"), work)
    target = Path(out) if out else output_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target


def cli():
    parser = argparse.ArgumentParser(description="把定稿產生成 HTML 報告")
    parser.add_argument("path", type=Path, help="定稿.md")
    parser.add_argument("--work", type=Path, help="工作夾，預設為定稿所在資料夾")
    parser.add_argument("--out", type=Path, help="輸出檔，預設為 報告/<代號>/<研究日>.html")
    arg = parser.parse_args()
    print(f"已產生：{render(arg.path, arg.work, arg.out)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        cli()
    except ValueError as error:
        sys.exit(str(error))
