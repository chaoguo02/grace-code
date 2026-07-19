"""Patch memory/store.py to add SQLite support."""
import re

with open('memory/store.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add db_path to __init__
content = content.replace(
    'repo_path: str,\n        base_dir: str | None = None,',
    'repo_path: str,\n        db_path: str | None = None,\n        base_dir: str | None = None,',
    1
)

# 2. Add self._db_path = db_path before _ensure_dir, guard MetadataCache
content = content.replace(
    'self._access_count_cache: dict[str, int] = {}  # deferred access_count increments\n        self._ensure_dir()\n        # ── Phase 6: In-memory metadata cache ──\n        from memory.metadata_cache import MetadataCache\n        self._metadata_cache = MetadataCache()\n        self._metadata_cache.build(self._store_dir)',
    'self._access_count_cache: dict[str, int] = {}  # deferred access_count increments\n        self._db_path = db_path\n        self._ensure_dir()\n        if not db_path:\n            from memory.metadata_cache import MetadataCache\n            self._metadata_cache = MetadataCache()\n            self._metadata_cache.build(self._store_dir)',
    1
)

# 3. Add _db_conn method before properties
content = content.replace(
    '    # ------------------------------------------------------------------\n    # 属性\n    # ------------------------------------------------------------------',
    '''    def _db_conn(self):
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------''',
    1
)

# 4. Modify read_memory to dispatch to SQLite
old_read_header = '''    def read_memory(self, name: str) -> Memory | None:
        \"\"\"
        读取一条记忆。

        Args:
            name: 记忆名称（slug），对应 {name}.md

        Returns:
            Memory 对象，不存在时返回 None
        \"\"\"
        path = self._file_path(name)'''

if old_read_header in content:
    content = content.replace(old_read_header,
        '    def read_memory(self, name: str) -> Memory | None:\n        if self._db_path:\n            return self._db_read_memory(name)\n        path = self._file_path(name)',
        1)
    print('4. read_memory dispatched')
else:
    print('4. WARN: read_memory header not found')

# 5. Add _db_read_memory before lifecycle section
lifecycle_marker = '    # ------------------------------------------------------------------\n    # 生命周期管理\n    # ------------------------------------------------------------------'

db_read_method = '''    def _db_read_memory(self, name: str) -> Memory | None:
        try:
            with self._db_conn() as conn:
                row = conn.execute("SELECT * FROM memory_entries WHERE name=?", (name,)).fetchone()
                if row is None:
                    return None
                anchors = []
                for a in conn.execute("SELECT * FROM memory_anchors WHERE memory_name=?", (name,)).fetchall():
                    anchor = Anchor(kind=a["kind"])
                    if a["path"]: anchor.path = a["path"]
                    if a["symbol_name"]: anchor.name = a["symbol_name"]
                    if a["task_value"]: anchor.value = a["task_value"]
                    if a["content_hash"]: anchor.content_hash = a["content_hash"]
                    anchors.append(anchor)
                return Memory(
                    name=row["name"], description=row["description"],
                    content=row["content"],
                    metadata=MemoryMetadata(
                        type=MemoryType(row["type"]) if row["type"] in ("user","feedback","project","reference") else MemoryType.PROJECT,
                        status=MemoryStatus(row["status"]) if row["status"] in ("active","deprecated") else MemoryStatus.ACTIVE,
                        scope=MemoryScope(row["scope"]) if row["scope"] in ("session","project","global") else MemoryScope.PROJECT,
                        confidence=row["confidence"], access_count=row["access_count"],
                    ),
                    updated_at=row["updated_at"], anchors=anchors,
                )
        except Exception as exc:
            logger.warning("DB read_memory %s failed: %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------'''

if lifecycle_marker in content:
    content = content.replace(lifecycle_marker, db_read_method, 1)
    print('5. _db_read_memory added')
else:
    print('5. WARN: lifecycle marker not found')

# 6. Modify write_memory to accept source= and dispatch
old_write = '''    def write_memory(self, memory: Memory) -> bool:'''
if old_write in content:
    content = content.replace(old_write,
        '    def write_memory(self, memory: Memory, source: str = "") -> bool:\n        if self._db_path:\n            return self._db_write_memory(memory, source=source)',
        1)
    print('6. write_memory dispatched')

# 7. Modify delete_memory to dispatch
old_delete = '''    def delete_memory(self, name: str) -> bool:
        \"\"\"
        删除一条记忆。

        Args:
            name: 记忆名称（slug）

        Returns:
            True 表示成功（文件不存在也返回 True）
        \"\"\"
        path = self._file_path(name)'''
if old_delete in content:
    content = content.replace(old_delete,
        '    def delete_memory(self, name: str) -> bool:\n        if self._db_path:\n            try:\n                with self._db_conn() as conn:\n                    conn.execute("DELETE FROM memory_anchors WHERE memory_name=?", (name,))\n                    conn.execute("DELETE FROM memory_entries WHERE name=?", (name,))\n                if self._indexer is not None:\n                    try: self._indexer.remove_memory(name)\n                    except Exception: pass\n                return True\n            except Exception as exc:\n                logger.error("DB delete_memory %s failed: %s", name, exc)\n                return False\n        path = self._file_path(name)',
        1)
    print('7. delete_memory dispatched')

with open('memory/store.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('All patches applied')
