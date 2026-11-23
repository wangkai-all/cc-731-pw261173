"""Pause / resume a conversion across a real process boundary.

    python3 scripts/demo_resume.py [--root demo-data]

Phase 1 runs in a child process that starts a deliberately slow conversion
and is then killed with SIGKILL mid-chunk.  Phase 2 starts a fresh process
pointing at the same directory, shows the interrupted progress that survived
on disk, resumes from the checkpoint and verifies the finished layout.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


WORKER = r"""
import json, sys, time
sys.path.insert(0, {here!r})
from app.sparse import Repository
repo = Repository({root!r})
records = [{{"row": f"R{{i // 6}}", "col": f"C{{i % 6}}", "value": i + 1}} for i in range(240)]
vid = repo.ingest(records, label="断点续转演示")["version_id"]
repo.start_conversion(vid, "csr", budget_bytes=256, pace_ms=120)
print(json.dumps({{"version_id": vid, "pid": __import__("os").getpid()}}), flush=True)
time.sleep(60)
"""


def run_child(root: str) -> dict:
    code = WORKER.format(here=HERE, root=root)
    child = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline()
    info = json.loads(line)
    time.sleep(1.6)
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=10)
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=tempfile.mkdtemp(prefix="sparse-resume-demo-"))
    args = parser.parse_args()
    root = os.path.abspath(args.root)

    print(f"工作目录：{root}")
    info = run_child(root)
    print(f"子进程 {info['pid']} 在转换中途被 SIGKILL 终止（数据版 #{info['version_id']}）。")

    from app.sparse import Repository

    repo = Repository(root)
    lay = repo.state["layouts"][info["version_id"]]["csr"]
    print(f"重新打开仓库后该布局状态：{lay['state']}（原因：{lay['pause_reason']}）")
    print(f"磁盘上保存的进度：{lay['progress']['next_index']}/{lay['progress']['total']}"
          f"，已写批次数 {lay['progress']['chunks_done']}")
    if lay["state"] != "paused":
        raise SystemExit("被测场景没有停在可恢复状态")
    repo.resume_conversion(info["version_id"], "csr")
    state = repo.wait_for_layout(info["version_id"], "csr", timeout=30)
    print(f"恢复后状态：{state['state']}，nnz={state['nnz']}，"
          f"批次数 {state['progress']['chunks_done']}")
    report = repo.verify_layout(info["version_id"], "csr")
    print(f"逐坐标核对：{'通过' if report['ok'] else '失败'}，"
          f"比对 {report['coordinate_checks']} 个坐标，digest {report['digest'][:16]}…")
    comparison = repo.compare(info["version_id"], "csr")
    print(f"与逻辑三元组对照：{'完全一致' if comparison['all_match'] else '存在差异'}")
    if not (report["ok"] and comparison["all_match"]):
        raise SystemExit(1)
    repo.close()


if __name__ == "__main__":
    main()
