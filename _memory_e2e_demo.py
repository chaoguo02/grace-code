"""
端到端记忆系统体验脚本
模拟完整会话生命周期：会话计数 → 锁竞争 → 原子写入 → 类型解析 → consolidation 三门判断
直接运行：python _memory_e2e_demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from memory.consolidation import (  # noqa: E402
    _acquire_lock,
    _read_session_counter,
    _release_lock,
    record_session_end,
)
from memory.models import parse_memory_type  # noqa: E402
from memory.store import _atomic_write_text  # noqa: E402


tmp = tempfile.mkdtemp(prefix="forge_memory_demo_")
memory_dir = Path(tmp)
print(f"[DEMO] memory_dir = {memory_dir}\n")

print("=" * 60)
print("场景 1：会话计数（模拟 3 次会话结束）")
print("=" * 60)

for i in range(1, 4):
    record_session_end(memory_dir)
    count = _read_session_counter(memory_dir)
    print(f"  会话 {i} 结束 → 计数器 = {count}")

counter_file = memory_dir / ".sessions-since-dream"
assert counter_file.exists(), "计数器文件应存在"
print(f"  OK 持久化文件: {counter_file.name}")
print(f"  OK 计数器值: {counter_file.read_text(encoding='utf-8').strip()}")
print()

print("=" * 60)
print("场景 2：锁获取 → 持有 → 释放 → 重获取")
print("=" * 60)

lock_path = memory_dir / ".consolidate-lock"

ok1 = _acquire_lock(lock_path)
print(f"  第一次 acquire: {ok1}")
content = lock_path.read_text(encoding="utf-8").strip()
print(f"  锁内容: {content}")
assert ok1 is True

_release_lock(lock_path)
assert lock_path.exists(), "释放后文件应保留"
assert lock_path.read_text(encoding="utf-8").strip() == "", "释放后内容应为空"
print(f"  释放后文件存在: {lock_path.exists()}")
print("  释放后内容为空: True")

ok2 = _acquire_lock(lock_path)
print(f"  第二次 acquire: {ok2}")
assert ok2 is True

_release_lock(lock_path)
print("  OK 锁生命周期完整")
print()

print("=" * 60)
print("场景 3：原子写入（模拟 MEMORY.md 重建）")
print("=" * 60)

target = memory_dir / "MEMORY.md"
_atomic_write_text(target, "# Memory Index\n\n- user/001.md\n- feedback/002.md\n")
files_after = [f for f in os.listdir(memory_dir) if ".tmp" in f]
print(f"  写入目标: {target.name}")
print(f"  残留 .tmp 文件数: {len(files_after)}")
assert len(files_after) == 0, "不应有 .tmp 残留"
content = target.read_text(encoding="utf-8")
print(f"  文件内容:\n    {content.strip()}")
print("  OK 原子写入无残留")
print()

print("=" * 60)
print("场景 4：记忆文件原子写入 + 类型解析")
print("=" * 60)

mem_dir = memory_dir / "memories" / "user"
mem_dir.mkdir(parents=True, exist_ok=True)
mem_file = mem_dir / "001.md"

old_frontmatter = (
    "---\n"
    "id: mem-001\n"
    "type: fact\n"
    "created: 2026-06-30\n"
    "---\n"
    "用户偏好深色主题\n"
)
_atomic_write_text(mem_file, old_frontmatter)
print(f"  写入旧/未知类型记忆: {mem_file.relative_to(memory_dir)}")
print("  frontmatter type: fact")

readback = mem_file.read_text(encoding="utf-8")
assert "type: fact" in readback
print("  回读验证: OK")

parsed = parse_memory_type({"type": "fact"})
print(f"  parse_memory_type({{'type': 'fact'}}) → '{parsed}'")
assert parsed == "project"
print("  OK 未知类型按当前实现兜底为 project")
print()

print("=" * 60)
print("场景 5：三门判断逻辑演示")
print("=" * 60)

count = _read_session_counter(memory_dir)
print(f"  当前会话计数: {count}")
print("  时间门: 锁文件 mtime 需 > 24h → ", end="")

if lock_path.exists():
    mtime = lock_path.stat().st_mtime
    age_hours = (time.time() - mtime) / 3600
    print(f"锁文件年龄 {age_hours:.1f}h → {'通过' if age_hours >= 24 else '未通过（< 24h）'}")
else:
    print("无锁文件 → 通过（首次运行）")

print(f"  会话门: 计数 >= 5 → {'通过' if count >= 5 else '未通过'}（当前 {count}）")
print("  锁门: 锁文件为空 → 可获取")
print()

print("=" * 60)
print("场景 6：补充会话使计数达到 5")
print("=" * 60)

for i in range(4, 6):
    record_session_end(memory_dir)
    count = _read_session_counter(memory_dir)
    print(f"  会话 {i} 结束 → 计数器 = {count}")

final_count = _read_session_counter(memory_dir)
print(f"  最终计数: {final_count}")
print(f"  会话门条件（>= 5）: {'OK 满足' if final_count >= 5 else 'FAIL 不满足'}")
print()

print("=" * 60)
print("端到端验证总结")
print("=" * 60)

checks = [
    ("会话计数持久化", counter_file.exists() and int(counter_file.read_text().strip()) == 5),
    ("锁文件保留+清空", lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == ""),
    ("原子写无残留", len([f for f in os.listdir(memory_dir) if ".tmp" in f]) == 0),
    ("记忆文件可读写", mem_file.exists() and "用户偏好深色主题" in mem_file.read_text(encoding="utf-8")),
    ("会话门可达", _read_session_counter(memory_dir) >= 5),
]

all_pass = True
for name, result in checks:
    status = "PASS" if result else "FAIL"
    print(f"  {status}  {name}")
    if not result:
        all_pass = False

print()
if all_pass:
    print("ALL E2E CHECKS PASSED — 记忆系统核心功能全部就绪")
else:
    print("部分检查未通过，请检查上方输出")

print(f"\n[DEMO] 临时目录: {memory_dir}")
print("[DEMO] 可手动查看: dir /s", memory_dir)
print("[DEMO] 清理: rmdir /s /q", memory_dir)
