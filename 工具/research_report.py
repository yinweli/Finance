import argparse
import json
import os
import re
import sys
import tempfile
from html import escape
from itertools import count
from pathlib import Path

from research_data import (
    MARKET, ReportParser, merge, parse_json, prepare, require, safe_url,
    snapshot, source_refs, subject_name, timestamp, text_value,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "版型" / "研究報告.html"
COLOR = ("#16665b", "#9c4b3f", "#2b65a0", "#715684", "#665923")
KIND_LABEL = {
    "verified": "已核實", "guidance": "公司正式預測", "consensus": "外部共識",
    "assumption": "研究假設", "unverified": "待核實",
}
CASE_LABEL = {"bear": "保守", "base": "中性", "bull": "樂觀"}


def json_text(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)


class Renderer:
    def __init__(self, data):
        self.data = data
        self.measure = data["input"] | data["assumption"] | data["result"]
        self.source = data["source"]

    def metric(self, key):
        entry = self.measure[key]
        value = entry["value"]
        if value is None:
            return "資料不足"
        unit = entry["unit"]
        precision = entry.get("precision", 2)
        number = f"{value:,.{precision}f}"
        return f"{number}{unit}" if unit in ("%", "‰") else f"{number} {unit}"

    def text(self, value):
        require(isinstance(value, str), "報告文字須為字串")
        value = re.sub(r"\[\[([a-zA-Z0-9_-]+)\]\]", lambda match: self.metric(match[1]), value)
        return escape(value)

    def reference(self, key):
        return f'<a href="#source-{list(self.source).index(key) + 1}">{escape(self.source[key]["name"])}</a>'

    def evidence(self, ref):
        return "、".join(self.reference(key) for key in source_refs(ref, self.source))

    def table(self, title, column, row):
        require(isinstance(column, list) and column and isinstance(row, list)
                and all(isinstance(item, (list, tuple)) and len(item) == len(column) for item in row),
                "表格欄位與每列數量須相同")
        heading = "".join(f'<th scope="col">{self.text(value)}</th>' for value in column)
        body = "".join(
            "<tr>" + "".join(f"<td>{self.text(value)}</td>" for value in item) + "</tr>"
            for item in row
        )
        return (
            f'<div class="table-scroll" tabindex="0" aria-label="{self.text(title)}">'
            f'<table><caption>{self.text(title)}</caption><thead><tr>{heading}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>'
        )

    def chart(self, block):
        title, unit, period = (block[key] for key in ("title", "unit", "period"))
        label, series, style = (block[key] for key in ("labels", "series", "style"))
        require(text_value(title) and text_value(unit) and text_value(period), "圖表須有標題、單位與期間")
        require(isinstance(label, list) and label and all(text_value(item) for item in label)
                and len(set(label)) == len(label), "圖表標籤須唯一且有文字")
        require(isinstance(series, dict) and series and style in ("bar", "line", "range"), "圖表系列或類型有誤")
        number = {}
        references = set(source_refs(block["source"], self.source))
        mark = block.get("reference")
        require(mark is None or style == "range", "基準線僅適用區間圖")
        mark_value = self.measure[mark]["value"] if mark is not None else None
        if mark is not None:
            require(self.measure[mark]["unit"] == unit, "基準線單位與圖表不同")
            references.update(self.measure[mark].get("source", []))
        for name, key in series.items():
            require(text_value(name) and isinstance(key, list) and len(key) == len(label),
                    f"圖表系列點數不同 {name}")
            number[name] = []
            for cell in key:
                if cell is None:
                    number[name].append(None)
                    continue
                ref = cell if style == "range" else [cell]
                require(isinstance(ref, list) and len(ref) == (2 if style == "range" else 1),
                        "區間圖須提供下限及上限兩個數值 ID")
                for identifier in ref:
                    entry = self.measure[identifier]
                    require(entry["unit"] == unit, f"圖表單位不符 {identifier}")
                    references.update(entry.get("source", []))
                value = [self.measure[identifier]["value"] for identifier in ref]
                if None in value:
                    number[name].append(None)
                elif style == "range":
                    require(value[0] <= value[1], "圖表區間下限高於上限")
                    number[name].append(tuple(value))
                else:
                    number[name].append(value[0])
        require(references, "圖表須標示來源")
        caption = f'{self.text(unit)} · {self.text(period)} · {self.evidence(sorted(references))}'
        row = [
            [text] + [
                ("–".join(f"[[{key}]]" for key in keys[index]) if style == "range" else f"[[{keys[index]}]]")
                if number[name][index] is not None else "資料不足"
                for name, keys in series.items()
            ]
            for index, text in enumerate(label)
        ]
        if mark is not None:
            caption += f'。市價參考 {self.metric(mark)}'
        table = self.table(f"{title}數值", ["項目", *series], row)
        if style == "range":
            display = [item[1:] for item in row]
            plot = range_plot(label, number, mark_value, display, self.text)
        else:
            plot = self.point_plot(block, label, number)
        return (
            f'<figure><h3>{self.text(title)}</h3>{plot}'
            f'<figcaption>{caption}</figcaption>'
            f'<details><summary>查看圖表數值</summary>{table}</details></figure>'
        )

    def point_plot(self, block, label, number):
        style = block["style"]
        available = [value for row in number.values() for value in row if value is not None]
        if not available:
            return "<p>目前沒有可繪製的資料。</p>"
        if style == "bar":
            available.append(0)
        low, high = min(available), max(available)
        scale = high - low or abs(high) or 1
        width = max(600, 35 * len(label) + 82)
        step = (width - 86) / len(label)

        def x(index):
            return 65 + step * (index + .5)

        def y(value):
            return 218 - 186 * (value - low) / scale

        shape = [f'<svg class="wide-chart" viewBox="0 0 {width} 253" role="group" aria-label="{self.text(block["title"])}">']
        forecast = block.get("forecast_from")
        if forecast is not None:
            require(forecast in label, "圖表預估起點須對應標籤")
            start = x(label.index(forecast)) - step / 2
            shape.append(f'<rect x="{start:.2f}" y="15" width="{width-start-12:.2f}" height="204" fill="#edf4f1"/>')
            shape.append(f'<text x="{start+3:.2f}" y="27">預估</text>')
        for index in range(5):
            value = low + scale * index / 4
            line = y(value)
            shape.append(f'<path d="M65 {line:.2f}H{width-12}" stroke="#d5e1de"/>')
            shape.append(f'<text x="62" y="{line+4:.2f}" text-anchor="end">{value:,.1f}</text>')
        for index, name in enumerate(label):
            shape.append(f'<text x="{x(index):.2f}" y="239" text-anchor="middle">{self.text(name)}</text>')
        width_bar = min(30, step * .75 / len(number))
        for index, (name, item) in enumerate(number.items()):
            color = COLOR[index % len(COLOR)]
            previous = None
            for position, value in enumerate(item):
                if value is None:
                    previous = None
                    continue
                horizontal, vertical = x(position), y(value)
                key = block["series"][name][position]
                note = self.text(f"{label[position]} · {name} · [[{key}]]")
                common = f'tabindex="0" role="button" aria-label="{note}" data-note="{note}" data-value="{value}"'
                if style == "bar":
                    offset = (index - len(number) / 2) * width_bar
                    shape.append(
                        f'<rect {common} x="{horizontal+offset:.2f}" y="{min(vertical,y(0)):.2f}" '
                        f'width="{width_bar:.2f}" height="{max(1, abs(y(0)-vertical)):.2f}" fill="{color}"/>'
                    )
                else:
                    if previous:
                        start_x, start_y = previous
                        shape.append(f'<path d="M{start_x:.2f} {start_y:.2f}L{horizontal:.2f} {vertical:.2f}" stroke="{color}" stroke-width="2"/>')
                    shape.append(f'<circle {common} cx="{horizontal:.2f}" cy="{vertical:.2f}" r="4" fill="{color}"/>')
                    previous = horizontal, vertical
        for event in block.get("events", []):
            require(event["at"] in label, "圖表事件須對應標籤")
            item = self.data["events"][event["index"]]
            horizontal = x(label.index(event["at"]))
            note = self.text(f'{item["date"]} · {item["title"]} · {KIND_LABEL[item["status"]]}')
            shape.append(f'<circle tabindex="0" role="button" aria-label="{note}" data-note="{note}" '
                         f'cx="{horizontal:.2f}" cy="16" r="6" fill="#8c5314"/>')
        legend = " ".join(
            f'<span><b style="color:{COLOR[index % len(COLOR)]}">●</b> {self.text(name)}</span>'
            for index, name in enumerate(number)
        )
        return f'<div class="chart-scroll">{"".join(shape)}</svg></div><p class="chart-legend">{legend}</p><p class="note" data-chart-note aria-live="polite">選取資料點查看數值。</p>'

    def blocks(self, identifier):
        output = []
        for block in self.data["sections"].get(identifier, []):
            kind, title = block["type"], block["title"]
            reference = self.evidence(block.get("source", []))
            if kind == "chart":
                content = self.chart(block)
            elif kind == "table":
                content = self.table(title, block["columns"], block["rows"])
            elif kind == "text":
                paragraph = block["text"].splitlines()
                content = f'<h3>{self.text(title)}</h3>' + "".join(f'<p>{self.text(line)}</p>' for line in paragraph if line.strip())
            else:
                raise ValueError(f"未知報告區塊類型 {kind}")
            if reference and kind != "chart":
                content += f'<p class="note">來源 {reference}</p>'
            if block.get("collapsed"):
                content = f'<details><summary>{self.text(title)}</summary>{content}</details>'
            output.append(content)
        return "".join(output)

    def evidence_detail(self):
        row = []
        for key, item in (self.data["input"] | self.data["assumption"]).items():
            source = self.evidence(item.get("source", []))
            date = self.text(item.get("observed_at", item["date"]).replace("T", " ", 1))
            row.append(
                f'<tr><th scope="row">{self.text(item.get("label", key))}</th><td>{self.metric(key)}</td>'
                f'<td>{KIND_LABEL[item["kind"]]}</td><td>{date}</td><td>{source}</td>'
                f'<td>{self.text(item.get("note", ""))}</td></tr>'
            )
        if not row:
            return ""
        return (
            '<details><summary>數值與證據明細</summary><div class="table-scroll" tabindex="0" aria-label="數值與證據">'
            '<table><thead><tr><th>項目</th><th>數值</th><th>證據</th><th>日期</th><th>來源</th>'
            f'<th>說明</th></tr></thead><tbody>{"".join(row)}</tbody></table></div></details>'
        )

    def market_sections(self):
        value = self.data["valuation"]
        fair = "[[" + value["fair"] + "]]"
        if "fair_high" in value:
            fair += "–[[" + value["fair_high"] + "]]"
        analysis = self.table("估值基準", ["項目", "結果"], [
            ["市價", "[[" + value["as_of_price"] + "]]"], ["今日合理價", fair],
            ["主要估值法", value["method"]], ["交叉核對", value["cross_check"]],
        ])
        if self.data.get("thesis"):
            thesis = self.data["thesis"]
            analysis += f'<h3>投資論點與反證</h3><p>{self.text(thesis["view"])}</p><p>{self.text(thesis["counter"])}</p>'
        row = []
        for key, label in CASE_LABEL.items():
            case = self.data["scenarios"][key]
            price = "[[" + case["price"] + "]]"
            price_change = f"[[case_{key}_price_change]]"
            outcome = "[[" + case["return"] + "]]"
            if "price_high" in case:
                price += "–[[" + case["price_high"] + "]]"
                price_change += f"–[[case_{key}_price_change_high]]"
                outcome += "–[[" + case["return_high"] + "]]"
            row.append([label, case["end_date"], price, price_change, "[[" + case["distribution"] + "]]", outcome, case["summary"]])
        scenarios = self.table("三種情境", ["情境", "期末日", "期末價", "價格漲跌幅", "股利或配息", "含息報酬", "假設"], row)
        return analysis, scenarios

    def render(self):
        data = self.data
        review = "".join(
            f'<li><strong>{self.text(item["trigger"])}</strong>　{self.text(item["action"])}</li>'
            for item in data["review"]
        )
        analysis, scenarios = self.market_sections() if data["asset_type"] in MARKET else ("", "")
        events = "".join(
            f'<li>{self.text(item["date"])} · {KIND_LABEL[item["status"]]} · '
            f'{self.text(item["title"])} {self.text(item.get("note", ""))} {self.evidence(item.get("source", []))}</li>'
            for item in data.get("events", [])
        )
        update = ""
        if "update" in data:
            item = data["update"]
            status = {"unchanged": "維持", "revised": "調整", "invalidated": "失效", "not_applicable": "不適用"}
            update = f'<h3>與前次研究的差異</h3><p>前次 {self.text(item.get("prior_report", ""))}。論點{status[item["thesis"]]}。{self.text(item["reason"])}</p>'
            update += "<ul>" + "".join(f"<li>{self.text(change)}</li>" for change in item["changes"]) + "</ul>"
        source = []
        for index, item in enumerate(self.source.values(), 1):
            name = self.text(item["name"])
            link = f'<a href="{escape(safe_url(item["url"]))}">{name}</a>' if "url" in item else name
            detail = " · ".join(self.text(item[key]) for key in ("data_date", "published_at", "locator") if key in item)
            source.append(f'<li id="source-{index}">{link} · {detail}</li>')
        limits = "".join(f"<li>{self.text(item)}</li>" for item in data["limits"])
        metrics = "".join(
            f"<div><dt>{self.text(label)}</dt><dd>{self.metric(key)}</dd></div>"
            for label, key in data["metrics"].items()
        )
        data_body = self.blocks("data")
        if "valuation_at" in data:
            data_body = f'<p class="meta">估值時點 {self.text(data["valuation_at"].replace("T", " ", 1))}</p>' + data_body
        fields = {
            "title": self.text(data["subject"]), "conclusion": self.text(data["conclusion"]),
            "as_of": escape(data["as_of"]), "data_cutoff": escape(data["data_cutoff"].replace("T", " ", 1)),
            "metrics": metrics, "next_review": self.text(data["review"][0]["trigger"]),
            "data_body": data_body + self.evidence_detail(),
            "analysis_body": analysis + self.blocks("analysis"),
            "scenarios_body": scenarios + self.blocks("scenarios"),
            "review_body": update + f"<ul>{review}</ul>" + (f"<h3>重大事件</h3><ul>{events}</ul>" if events else "") + self.blocks("review"),
            "sources_body": "<ol>" + "".join(source) + "</ol>" + (f"<h3>限制</h3><ul>{limits}</ul>" if limits else ""),
            "research_data": json_text(data).replace("<", "\\u003c"),
        }
        return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: fields[match[1]], TEMPLATE.read_text(encoding="utf-8-sig"))


def range_plot(label, number, mark, display, render_text):
    values = [value for row in number.values() for pair in row if pair is not None for value in pair]
    if not values:
        return "<p>目前沒有可繪製的資料。</p>"
    if mark is not None:
        values.append(mark)
    low, high = min(values), max(values)
    scale = high - low or 1
    output = []
    for index, text in enumerate(label):
        for lane, (name, row) in enumerate(number.items()):
            if row[index] is None:
                continue
            begin, end = row[index]
            shown = render_text(display[index][lane])
            note = f"{escape(text)} · {escape(name)} · {shown}"
            marker = "" if mark is None else f'<span class="range-market" style="left:{100*(mark-low)/scale:.4f}%"></span>'
            output.append(
                f'<div class="range-row"><span>{escape(text)} · {escape(name)}</span>'
                f'<div class="range-track"><button type="button" class="range-band" aria-label="{note}" data-note="{note}" '
                f'style="left:{100*(begin-low)/scale:.4f}%;width:{100*(end-begin)/scale:.4f}%;'
                f'background:{COLOR[lane % len(COLOR)]}"></button>{marker}</div>'
                f'<span>{shown}</span></div>'
            )
    return '<div class="range-chart">' + "".join(output) + '</div><p class="note" data-chart-note aria-live="polite">選取區間查看數值。</p>'


def check_markup(html):
    parser = ReportParser()
    parser.feed(html)
    parser.close()
    require(not parser.remote, f"HTML 包含外部資源 {parser.remote}")
    require(not parser.errors, "、".join(parser.errors))
    require(not parser.anchors - parser.ids, f"找不到內部連結 {parser.anchors - parser.ids}")
    require(len(parser.snapshot) == 1 and not parser.active, "缺少完整快照")


def publish(data, directory):
    data = prepare(data)
    markup = Renderer(data).render()
    check_markup(markup)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f'{data["as_of"]}-{subject_name(data["subject"])}'
    # 在同目錄完成檔案後用硬連結發布，避免同時執行時覆寫或留下半份報告。
    with tempfile.NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8", newline="\n",
                                     prefix=".research-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(markup)
            stream.close()
            for version in count(1):
                suffix = "" if version == 1 else f"-v{version}"
                path = directory / f"{stem}{suffix}.html"
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    continue
                return path
        finally:
            temporary.unlink(missing_ok=True)


def latest(directory, ticker, exchange, subject):
    pattern = re.compile(rf"(\d{{4}}-\d{{2}}-\d{{2}})-{re.escape(subject_name(subject))}(?:-v([2-9]|[1-9]\d+))?\.html")
    candidate = []
    for path in directory.glob("*.html"):
        match = pattern.fullmatch(path.name)
        if match:
            candidate.append((match[1], int(match[2] or 1), path))
    for day, _, path in sorted(candidate, reverse=True):
        try:
            data = snapshot(path)
            if data.get("schema_version") == 1 and (data["ticker"], data["exchange"], data["subject"], data["as_of"]) == (
                ticker, exchange, subject, day
            ):
                require(prepare(data)["result"] == data.get("result"), "快照結果和公式不一致")
                return path
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"略過 {path.name}：{error}", file=sys.stderr)
    raise ValueError("找不到同代號、交易所及主題的有效快照。請執行首次研究，或先補建相容快照。")


def select(data, field):
    output = {}
    for key in field:
        node = data
        for piece in key.split("."):
            require(isinstance(node, dict) and piece in node, f"快照無欄位 {key}")
            node = node[piece]
        output[key] = node
    return output


def cli():
    parser = argparse.ArgumentParser(description="Finance 離線報告與研究快照工具")
    command = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "update"):
        sub = command.add_parser(name)
        sub.add_argument("input", type=Path)
        sub.add_argument("--output-dir", default=ROOT / "報告", type=Path)
        if name == "update":
            sub.add_argument("--base", type=Path)
            for key in ("ticker", "exchange", "subject"):
                sub.add_argument(f"--{key}")
    for name in ("extract", "verify", "validate"):
        sub = command.add_parser(name)
        sub.add_argument("path", type=Path)
        if name == "extract":
            sub.add_argument("--field", action="append", default=[])
    find = command.add_parser("latest")
    find.add_argument("--output-dir", default=ROOT / "報告", type=Path)
    for key in ("ticker", "exchange", "subject"):
        find.add_argument(f"--{key}", required=True)
    arg = parser.parse_args()

    if arg.command == "latest":
        return latest(arg.output_dir, arg.ticker, arg.exchange, arg.subject)
    if arg.command == "extract":
        data = snapshot(arg.path)
        return json_text(select(data, arg.field) if arg.field else data)
    if arg.command == "verify":
        data = snapshot(arg.path)
        markup = arg.path.read_text(encoding="utf-8-sig")
        check_markup(markup)
        prepared = prepare(data)
        require(data["result"] == prepared["result"], "快照結果和公式不一致")
        require(markup == Renderer(prepared).render(), "HTML 與目前版型或快照數字不一致")
        return "HTML 已驗證"
    if arg.command == "validate":
        data = prepare(parse_json(arg.path.read_text(encoding="utf-8-sig")))
        check_markup(Renderer(data).render())
        return "研究資料已驗證"
    data = parse_json(arg.input.read_text(encoding="utf-8-sig"))
    if arg.command == "update":
        require(arg.base or all((arg.ticker, arg.exchange, arg.subject)), "請提供 --base 或代號、交易所和主題")
        base_path = arg.base or latest(arg.output_dir, arg.ticker, arg.exchange, arg.subject)
        base = snapshot(base_path)
        require(base.get("schema_version") == 1, "舊版快照須補建基線後才可合併更新")
        require(prepare(base)["result"] == base.get("result"), "基線公式與快照不一致")
        require(not data.keys() & {"schema_version", "subject", "ticker", "exchange", "asset_type", "result"},
                "增量更新不可修改標的識別、版本或計算結果")
        require(data["as_of"] >= base["as_of"] and timestamp(data["data_cutoff"]) >= timestamp(base["data_cutoff"]),
                "增量日期或資料截止時間早於基線")
        update = data["update"]
        data = merge(base, data)
        data["update"] = update | {"prior_report": base_path.name, "prior_as_of": base["as_of"]}
    return publish(data, arg.output_dir)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        print(cli())
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        sys.exit(f"研究報告處理失敗：{error}")
