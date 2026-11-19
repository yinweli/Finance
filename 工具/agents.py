"""以非互動方式呼叫 Claude Code 與 Codex，固定權限並保存紀錄。

- Claude：可讀任何檔案、可搜尋與讀網頁，只能寫入工作夾，不提供 Bash。
- Codex：可讀任何檔案、只能寫入工作夾，關閉網路，不載入使用者的外掛與 MCP。
"""
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

QUOTA = re.compile(r"usage limit|rate limit|limit reached|hit your limit|quota|429|too many requests|額度", re.I)
CLAUDE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch")


class AgentError(RuntimeError):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # error、quota、timeout


def program(name):
    path = shutil.which(name)
    if not path:
        raise AgentError(f"找不到 {name} 指令，請確認已安裝並在 PATH 中")
    return path


def save_log(work, step, name, content):
    folder = Path(work) / "紀錄"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    path = folder / f"{step}-{stamp}-{name}"
    path.write_text(content, encoding="utf-8", errors="replace")
    return path


def run(cmd, prompt, cwd, timeout):
    env = dict(os.environ)
    # 從 Claude Code 內啟動時，避免子程序誤判為巢狀工作階段
    env.pop("CLAUDECODE", None)
    try:
        done = subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AgentError(f"執行超過 {timeout // 60} 分鐘", "timeout") from None
    return done


def claude(prompt, work, step, tools=CLAUDE_TOOLS, timeout=1800):
    """在工作夾執行一次 claude -p，回傳最後的回覆文字。"""
    allow = [t for t in tools if t not in ("Write", "Edit")]
    allow += [f"{t}(./**)" for t in tools if t in ("Write", "Edit")]
    cmd = [program("claude"), "-p", "--output-format", "json", "--permission-mode", "dontAsk",
           "--tools", ",".join(tools), "--allowedTools", *allow,
           "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
           "--no-session-persistence"]
    save_log(work, step, "claude-prompt.md", prompt)
    done = run(cmd, prompt, work, timeout)
    save_log(work, step, "claude-output.json", done.stdout + ("\n--- stderr ---\n" + done.stderr if done.stderr else ""))
    try:
        data = json.loads(done.stdout)
    except ValueError:
        text = (done.stderr or done.stdout)[-800:]
        raise AgentError(f"Claude 回應無法解析：{text}", "quota" if QUOTA.search(text) else "error") from None
    if data.get("is_error") or done.returncode != 0:
        text = str(data.get("result", ""))[-800:]
        raise AgentError(f"Claude 執行失敗：{text}", "quota" if QUOTA.search(text) else "error")
    return data.get("result", ""), data.get("usage", {})


def codex_config():
    """沿用使用者設定的模型與 Windows 沙盒，但不載入外掛與 MCP。"""
    path = Path.home() / ".codex" / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    option = []
    if config.get("model"):
        option += ["-m", config["model"]]
    if config.get("model_reasoning_effort"):
        option += ["-c", f"model_reasoning_effort={config['model_reasoning_effort']}"]
    sandbox = (config.get("windows") or {}).get("sandbox")
    if sandbox:
        option += ["-c", f"windows.sandbox={sandbox}"]
    return option


def codex(prompt, work, step, thread=None, timeout=2400):
    """在工作夾執行 codex exec；thread 有值時接續同一個工作階段。回傳（最後回覆, thread_id）。"""
    last = Path(work) / "紀錄" / f"{step}-codex-last.txt"
    last.parent.mkdir(parents=True, exist_ok=True)
    common = ["--json", "--skip-git-repo-check", "--ignore-user-config", *codex_config(),
              "-c", "sandbox_mode=workspace-write", "-c", "sandbox_workspace_write.network_access=false",
              "-o", str(last)]
    base = [program("codex"), "exec"]
    cmd = base + ["resume", *common, thread, "-"] if thread else base + [*common, "-"]
    save_log(work, step, "codex-prompt.md", prompt)
    done = run(cmd, prompt, work, timeout)
    save_log(work, step, "codex-events.jsonl", done.stdout + ("\n--- stderr ---\n" + done.stderr[-4000:] if done.stderr else ""))
    found = re.search(r'"thread_id"\s*:\s*"([^"]+)"', done.stdout)
    thread = found.group(1) if found else thread
    failed = re.findall(r'"type"\s*:\s*"(?:turn\.failed|error)".*', done.stdout)
    if done.returncode != 0 or failed:
        text = ("\n".join(failed) or done.stderr or done.stdout)[-800:]
        raise AgentError(f"Codex 執行失敗：{text}", "quota" if QUOTA.search(text) else "error")
    return (last.read_text(encoding="utf-8") if last.exists() else ""), thread
