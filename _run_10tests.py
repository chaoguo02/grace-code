# -*- coding: utf-8 -*-
"""Run 10 verification tasks and produce a summary table."""
import json
import subprocess
import sys
import io
import time
import os
import shutil
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
os.chdir(r"d:\gc\forge-agent")

REPO = "_test_project"
LOG_DIR = Path("logs")
SUBTASK_DIR = LOG_DIR / "subtasks"

# Backup project state for reset between destructive tasks
PROJECT_BACKUP = "_test_project_backup"

TASKS = [
    # Group 1: React (should NOT trigger plan)
    {"id": "R1", "mode": "react", "intent": "edit",
     "task": "把 config.py 里的 SECRET_KEY 默认值换成一个随机字符串"},
    {"id": "R2", "mode": "react", "intent": "analysis",
     "task": "项目里用了哪些第三方依赖？"},
    # Group 2: v2-plan single file edit
    {"id": "P1", "mode": "v2-plan", "intent": "edit",
     "task": "给 UserService 添加一个 delete_user(user_id) 方法，并在 UserRouter 中新增 DELETE /users/<user_id> 端点"},
    {"id": "P2", "mode": "v2-plan", "intent": "edit",
     "task": "给 UserRouter 的 POST /users 端点加上请求参数校验：username 不能为空且长度 3-20，email 必须是合法邮箱格式"},
    # Group 3: v2-plan multi-file edit (loop detection stress)
    {"id": "M1", "mode": "v2-plan", "intent": "edit",
     "task": "把项目中所有使用 print() 输出的地方都改成 logger.debug()，每个文件头部确保有 logger = logging.getLogger(__name__)"},
    {"id": "M2", "mode": "v2-plan", "intent": "edit",
     "task": "给所有 API 端点加上统一的 try-except 错误处理，捕获异常后返回 JSON 格式的错误响应"},
    # Group 4: V2-Plan two-phase flow
    {"id": "V1", "mode": "v2-plan", "intent": "auto",
     "task": "分析项目现有的数据库模型，然后创建一个 db/migrate.py 脚本，自动生成建表 SQL"},
    {"id": "V2", "mode": "v2-plan", "intent": "auto",
     "task": "把项目中所有直接 import models 的地方改成通过 services 层间接访问，保持现有功能不变"},
    # Group 5: Edge cases
    {"id": "E1", "mode": "v2-plan", "intent": "auto",
     "task": "优化一下这个项目的代码质量"},
    {"id": "E2", "mode": "v2-plan", "intent": "edit",
     "task": "修改 src/nonexistent.py 里的 main 函数"},
]


def get_log_snapshot():
    files = set()
    if LOG_DIR.exists():
        files.update(str(f) for f in LOG_DIR.glob("*.jsonl"))
    if SUBTASK_DIR.exists():
        files.update(str(f) for f in SUBTASK_DIR.glob("*.jsonl"))
    return files


def get_new_logs(before):
    new_main, new_sub = [], []
    if LOG_DIR.exists():
        for f in LOG_DIR.glob("*.jsonl"):
            if str(f) not in before:
                new_main.append(f)
    if SUBTASK_DIR.exists():
        for f in SUBTASK_DIR.glob("*.jsonl"):
            if str(f) not in before:
                new_sub.append(f)
    return sorted(new_main), sorted(new_sub)


def analyze_logs(main_logs, sub_logs):
    """Extract status and tool call count from logs."""
    status = "unknown"
    tool_call_count = 0
    has_plan = False

    for lf in main_logs + sub_logs:
        try:
            with open(lf, encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    e = json.loads(line)
                    et = e.get("event_type", "")
                    if et == "plan_generated":
                        has_plan = True
                    if et == "action":
                        action = (e.get("payload") or {}).get("action") or {}
                        tool_call_count += len(action.get("tool_calls", []))
                    if et == "task_complete":
                        status = "PASS"
                    if et == "task_failed":
                        reason = (e.get("payload") or {}).get("reason", "")
                        if "Token budget" in reason:
                            status = "BUDGET"
                        elif "Loop" in reason:
                            status = "LOOP"
                        elif "consecutive" in reason:
                            status = "FAIL_3X"
                        else:
                            status = "GAVE_UP"
        except Exception:
            pass

    return status, tool_call_count, has_plan


def run_task(task_info):
    before = get_log_snapshot()
    start = time.time()

    cmd = [
        "python", "-m", "entry.cli", "run",
        "--mode", task_info["mode"],
        "--repo", REPO,
        "-t", task_info["task"],
        "--auto-approve",
    ]
    if task_info["intent"] != "auto":
        cmd.extend(["--intent", task_info["intent"]])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        rc = result.returncode
    except subprocess.TimeoutExpired:
        output = ""
        rc = -1
    except Exception as ex:
        output = str(ex)
        rc = -2

    elapsed = time.time() - start
    new_main, new_sub = get_new_logs(before)
    status, tool_calls, has_plan = analyze_logs(new_main, new_sub)

    # Override status for v2-plan (plan_generated is in a separate session)
    if task_info["mode"] == "v2-plan" and "Plan auto-approved" in output:
        has_plan = True

    if rc == -1:
        status = "TIMEOUT"

    # For v2 modes, check output for success indicator
    if status == "unknown" and "completed successfully" in output:
        status = "SUCCESS"
    if status == "unknown" and "GAVE_UP" in output:
        status = "GAVE_UP"
    if status == "unknown" and rc == 0 and task_info["mode"] in ("v2-build", "v2-plan"):
        status = "OK"

    return {
        "id": task_info["id"],
        "elapsed": round(elapsed, 1),
        "tool_calls": tool_calls,
        "status": status,
        "has_plan": has_plan,
        "mode": task_info["mode"],
    }


def main():
    # Backup project for reset
    if os.path.exists(PROJECT_BACKUP):
        shutil.rmtree(PROJECT_BACKUP)
    shutil.copytree(REPO, PROJECT_BACKUP)

    results = []
    for i, task in enumerate(TASKS):
        print(f"[{i+1}/10] {task['id']}: {task['task'][:50]}...", flush=True)

        # Reset project state before each destructive task
        if task["id"] in ("M1", "M2", "V1", "V2", "E1"):
            shutil.rmtree(REPO)
            shutil.copytree(PROJECT_BACKUP, REPO)

        r = run_task(task)
        results.append(r)
        flag = "PASS" if r["status"] in ("SUCCESS", "success", "OK") else r["status"]
        print(f"       -> {r['elapsed']}s | {r['tool_calls']} calls | {flag}", flush=True)

    # Cleanup
    if os.path.exists(PROJECT_BACKUP):
        shutil.rmtree(PROJECT_BACKUP)

    # Summary
    print("\n" + "=" * 72)
    print(f"{'ID':<4} {'Mode':<9} {'Time':<7} {'Calls':<6} {'Plan':<5} {'Status'}")
    print("-" * 72)
    for r in results:
        plan_str = "Y" if r["has_plan"] else "N"
        print(f"{r['id']:<4} {r['mode']:<9} {r['elapsed']:<7} {r['tool_calls']:<6} {plan_str:<5} {r['status']}")

    print("-" * 72)
    success = sum(1 for r in results if r["status"] in ("SUCCESS", "success", "OK"))
    print(f"Result: {success}/10 passed")

    # Key checks
    print("\nKey Observations:")
    r1 = next(r for r in results if r["id"] == "R1")
    r2 = next(r for r in results if r["id"] == "R2")
    m1 = next(r for r in results if r["id"] == "M1")
    v1 = next(r for r in results if r["id"] == "V1")
    e1 = next(r for r in results if r["id"] == "E1")
    e2 = next(r for r in results if r["id"] == "E2")

    print(f"  R1/R2 no plan: {'PASS' if not r1['has_plan'] and not r2['has_plan'] else 'FAIL'}")
    print(f"  M1 no false loop: {'PASS' if m1['status'] != 'LOOP' else 'FAIL'} (status={m1['status']})")
    print(f"  V1 no timeout: {'PASS' if v1['status'] != 'TIMEOUT' else 'FAIL'} (status={v1['status']})")
    print(f"  E1 converges: {'PASS' if e1['status'] != 'TIMEOUT' else 'FAIL'} (status={e1['status']})")
    print(f"  E2 handles missing: {'PASS' if e2['status'] not in ('TIMEOUT', 'LOOP') else 'FAIL'} (status={e2['status']})")


if __name__ == "__main__":
    main()
