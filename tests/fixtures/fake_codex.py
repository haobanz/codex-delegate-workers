#!/usr/bin/env python3
"""测试专用的假 Codex CLI（明确标记为 fake，不做任何网络或模型调用）。

它模拟 ``codex exec`` 的可观察行为：把人类可读启动头部写到 stderr，把最终回答写到
``--output-last-message`` 指定的文件。行为由环境变量控制，便于测试各类真实场景：

    FAKE_CODEX_HEADER=normal|missing|mismatch-model|mismatch-effort|partial|unrecognized
    FAKE_CODEX_EXIT=0|3          # 退出码
    FAKE_CODEX_WRITE=1|0         # 是否写最终输出文件
    FAKE_CODEX_SLEEP=秒数        # 启动头部之后停留时间（用于取消与超时场景）
    FAKE_CODEX_SPAWN_CHILD=1     # 额外启动一个子进程，用于验证进程树终止
    FAKE_CODEX_PROMPT_COPY=路径   # 把 stdin 收到的 prompt 原样写入该文件
    FAKE_CODEX_STDERR_EXTRA=文本  # 在启动头部之后追加一行 stderr（用于验证不会误读）
    FAKE_CODEX_STDOUT_SPAM=1     # 向 stdout 输出干扰内容（协议污染检查）
    FAKE_CODEX_ARGV_COPY=路径     # 把实际收到的参数数组写入该文件
    FAKE_CODEX_CHILD_PID_FILE=路径  # 记录附带子进程的 PID
    FAKE_CODEX_STUBBORN_CHILD=1  # 附带一个忽略 SIGTERM 的子进程（进程组残留场景）
    FAKE_CODEX_DETACHED_CHILD=1  # 附带一个独立会话（setsid）的子进程：不能只靠进程组收尾
    FAKE_CODEX_INHERIT_STDERR_CHILD=1  # 组长立即退出，但附带子进程继续持有 stderr 管道
    FAKE_CODEX_OUTPUT_BYTES=字节数  # 最终输出写满指定字节数（用于验证有界读取与截断标记）
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FIXTURE_NOTE = "FAKE codex CLI（tests/fixtures/fake_codex.py）：没有网络或模型调用。"


def parse_arguments(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?")
    parser.add_argument("--model")
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("--sandbox")
    parser.add_argument("--color")
    parser.add_argument("-o", "--output-last-message")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--skip-git-repo-check", action="store_true")
    parser.add_argument("prompt", nargs="?")
    arguments, extra = parser.parse_known_args(argv)
    arguments.extra = extra
    return arguments


def effort_from_config(values):
    for value in values:
        if value.startswith("model_reasoning_effort="):
            raw = value.split("=", 1)[1]
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return parsed if isinstance(parsed, str) else str(parsed)
    return None


def write_header(stream, args, effort):
    mode = os.environ.get("FAKE_CODEX_HEADER", "normal")
    if mode == "missing":
        stream.write(FIXTURE_NOTE + "\n")
        return
    if mode == "unrecognized":
        # 未知 CLI 版本：没有任何可识别的启动头部结构。
        stream.write(FIXTURE_NOTE + "\n")
        stream.write("some unrelated banner line\n")
        stream.write("another line without the delimiter\n")
        return
    model = args.model
    recorded_effort = effort
    if mode == "mismatch-model":
        model = "fake/other-model"
    elif mode == "mismatch-effort":
        recorded_effort = "low"
    stream.write("OpenAI Codex v0.0.0-fake\n")
    stream.write("--------\n")
    stream.write(f"workdir: {os.getcwd()}\n")
    if mode != "partial":
        stream.write(f"model: {model}\n")
        stream.write("provider: fake-local\n")
        stream.write(f"sandbox: {args.sandbox}\n")
    if mode != "partial":
        stream.write(f"reasoning effort: {recorded_effort}\n")
        stream.write("reasoning summaries: none\n")
        stream.write("session id: fake-" + os.urandom(6).hex() + "\n")
    stream.write("--------\n")
    stream.flush()


def on_terminate(signum, _frame):
    print(f"{FIXTURE_NOTE} 收到信号 {signum}，退出。", file=sys.stderr, flush=True)
    sys.exit(143)


def main(argv=None):
    args = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    prompt = sys.stdin.read()
    copy_path = os.environ.get("FAKE_CODEX_PROMPT_COPY")
    if copy_path:
        Path(copy_path).write_text(prompt, encoding="utf-8")
    argv_copy = os.environ.get("FAKE_CODEX_ARGV_COPY")
    if argv_copy:
        Path(argv_copy).write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding="utf-8")
    if os.environ.get("FAKE_CODEX_STDOUT_SPAM") == "1":
        print("fake-codex: stdout 干扰内容（不应出现在 MCP stdout 协议流中）", flush=True)
    effort = effort_from_config(args.config)
    signal.signal(signal.SIGTERM, on_terminate)
    write_header(sys.stderr, args, effort)
    extra = os.environ.get("FAKE_CODEX_STDERR_EXTRA")
    if extra:
        # 位于启动头部之后：解析器不得把它当作新鲜元数据。
        print(extra, file=sys.stderr, flush=True)
    child = None
    if os.environ.get("FAKE_CODEX_SPAWN_CHILD") == "1":
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time\nwhile True: time.sleep(0.2)"])
    if os.environ.get("FAKE_CODEX_STUBBORN_CHILD") == "1":
        # 忽略 SIGTERM，并且与 CLI 同属一个进程组：只有强制终止整组才能收掉它。
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             "while True: time.sleep(0.2)"])
    if os.environ.get("FAKE_CODEX_DETACHED_CHILD") == "1":
        # 独立会话：与 CLI 不在同一进程组，只能按记录到的身份终止。
        child = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    inherits_stderr = os.environ.get("FAKE_CODEX_INHERIT_STDERR_CHILD") == "1"
    if inherits_stderr:
        # 继承 stderr 管道却不退出：组长结束后排空线程仍会被阻塞。
        child = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"])
    if child is not None:
        child_pid_file = os.environ.get("FAKE_CODEX_CHILD_PID_FILE")
        if child_pid_file:
            Path(child_pid_file).write_text(str(child.pid), encoding="utf-8")
    sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0") or 0)
    if sleep:
        time.sleep(sleep)
    keep_child = (os.environ.get("FAKE_CODEX_STUBBORN_CHILD") == "1"
                  or os.environ.get("FAKE_CODEX_DETACHED_CHILD") == "1"
                  or inherits_stderr)
    if child is not None and not keep_child:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            child.kill()
    if args.output_last_message and os.environ.get("FAKE_CODEX_WRITE", "1") == "1":
        filler = int(os.environ.get("FAKE_CODEX_OUTPUT_BYTES", "0") or 0)
        if filler:
            Path(args.output_last_message).write_bytes(b"x" * filler)
        else:
            Path(args.output_last_message).write_text(
                "FAKE 最终回答：任务已完成。\n"
                + json.dumps({"prompt_chars": len(prompt)}, ensure_ascii=False),
                encoding="utf-8")
    return int(os.environ.get("FAKE_CODEX_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
