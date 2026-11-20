"""更新 README.md 的「最新報告」區段：每個標的列出最新 3 份報告，連到 GitHub Pages。"""

import sys
from pathlib import Path

import render
from source import ROOT

README = ROOT / "README.md"
PAGES = "https://yinweli.github.io/Finance/"
KEEP = 3
BEGIN = "<!-- 最新報告開始：由 工具/readme.py 產生，請勿手動修改 -->"
END = "<!-- 最新報告結束 -->"


def table(folder=None):
    base = Path(folder or render.REPORT)
    tickers = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []
    rows = ["| 標的 | 最新報告 |", "| --- | --- |"]
    for ticker in tickers:
        report = render.reports(ticker, base)[-KEEP:][::-1]
        if not report:
            continue
        data = render.extract(report[0]) or {}
        name = data.get("關鍵數字", {}).get("名稱", "")
        link = "、".join(f"[{p.stem}]({PAGES}{ticker}/{p.name})" for p in report)
        rows.append(f"| {ticker} {name}".rstrip() + f" | {link} |")
    return "\n".join(rows) if len(rows) > 2 else "目前沒有報告。"


def update(path=None, folder=None):
    """改寫標記之間的內容；README 沒有標記時不動並回傳 False。"""
    path = Path(path or README)
    text = path.read_text(encoding="utf-8")
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        return False
    new = text[:start + len(BEGIN)] + "\n\n" + table(folder) + "\n\n" + text[end:]
    if new != text:
        path.write_text(new, encoding="utf-8")
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if update() else f"{README} 缺少標記：{BEGIN}")
