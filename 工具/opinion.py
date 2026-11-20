"""把操作意見的 Markdown 產生成單頁 HTML，存到 報告/意見/，版面沿用研究報告。

Markdown 第一行是 `# 標題`，標題到第一個 `## ` 之間是前言，每個 `## ` 是一個章節並列入選單。
"""
import argparse
import sys
from pathlib import Path

import render

FOLDER = render.REPORT / render.OPINION
SUBTITLE = "操作意見"
FOOTER = "本頁由 Claude 依研究報告與持股紀錄整理，內容僅供個人研究參考，不構成投資建議。"


def split(text):
    """回傳 (標題, 前言, [(章節名稱, 內容)])。"""
    lines = text.strip("\n").split("\n")
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else SUBTITLE
    intro, section, name, buf = [], [], None, []
    for line in lines[1:] if lines and lines[0].startswith("# ") else lines:
        if line.startswith("## "):
            if name is None:
                intro = buf
            else:
                section.append((name, buf))
            name, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if name is None:
        intro = buf
    else:
        section.append((name, buf))
    return title, "\n".join(intro), [(n, "\n".join(c)) for n, c in section]


def build(text):
    title, intro, section = split(text)
    nav, body = [], []
    if intro.strip():
        body.append(f'<section class="summary">{render.markdown(intro)}</section>')
    for index, (name, content) in enumerate(section):
        anchor = f"sec-{index}"
        nav.append(f'<a href="#{anchor}">{render.esc(name)}</a>')
        body.append(f'<section id="{anchor}"><h2>{render.esc(name)}</h2>{render.markdown(content)}</section>')
    template = render.TEMPLATE.read_text(encoding="utf-8")
    return (template.replace("{{title}}", render.esc(title))
            .replace("{{subtitle}}", SUBTITLE)
            .replace("{{nav}}", "".join(nav))
            .replace("{{body}}", "\n".join(body))
            .replace("{{footer}}", render.esc(FOOTER))
            .replace("{{data}}", ""))


def publish(path, out=None):
    path = Path(path)
    target = Path(out) if out else FOLDER / f"{path.stem}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build(path.read_text(encoding="utf-8")), encoding="utf-8")
    return target


def cli():
    parser = argparse.ArgumentParser(description="把操作意見 Markdown 產生成 HTML")
    parser.add_argument("path", type=Path, help="意見 Markdown，建議放在 .tmp/工作/意見/")
    parser.add_argument("--out", type=Path, help="輸出檔，預設為 報告/意見/<檔名>.html")
    arg = parser.parse_args()
    print(f"已產生：{publish(arg.path, arg.out)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cli()
