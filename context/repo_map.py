"""
context/repo_map.py

Repo-map：把整个 repo 的结构压缩成一段摘要字符串，注入 system prompt。

核心思路（简化版 Aider repo-map）：
1. 用 tree-sitter 扫描源码文件，提取函数/类定义
2. 用正则 fallback 处理 tree-sitter 不支持或未安装的语言
3. 按"重要性"排序：顶层定义 > 方法，文件越小越可能是核心文件
4. 按 token 预算截取，生成摘要字符串

## 多语言支持

tree-sitter 每种语言需要单独安装语言包：

    pip install tree-sitter-python       # Python（必装）
    pip install tree-sitter-javascript   # JavaScript
    pip install tree-sitter-typescript   # TypeScript
    pip install tree-sitter-go           # Go
    pip install tree-sitter-rust         # Rust
    pip install tree-sitter-java         # Java

未安装的语言自动降级为正则解析，不报错。
新增语言只需在 _LANG_REGISTRY 里加一行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 语言注册表
# 格式：文件扩展名 → (pip 包名, 模块属性名)
# 运行时按需 import，失败时静默跳过，降级为正则
# ---------------------------------------------------------------------------

_LANG_REGISTRY: dict[str, tuple[str, str]] = {
    ".py":  ("tree_sitter_python",     "language"),
    ".js":  ("tree_sitter_javascript", "language"),
    ".ts":  ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go":  ("tree_sitter_go",         "language"),
    ".rs":  ("tree_sitter_rust",       "language"),
    ".java":("tree_sitter_java",       "language"),
    ".cpp": ("tree_sitter_cpp",        "language"),
    ".c":   ("tree_sitter_c",          "language"),
    ".rb":  ("tree_sitter_ruby",       "language"),
}

# AST 节点类型 → symbol kind 映射（各语言通用名）
_FUNC_NODES: frozenset[str] = frozenset({
    "function_definition",       # Python, Go, C, C++
    "async_function_definition", # Python async def
    "function_declaration",      # JS, TS, Java
    "method_declaration",        # Java
    "method_definition",         # JS class method
    "function_item",             # Rust fn
    "arrow_function",            # JS arrow（跳过，通常是匿名的）
})
_CLASS_NODES: frozenset[str] = frozenset({
    "class_definition",   # Python
    "class_declaration",  # JS, TS, Java
    "struct_item",        # Rust struct
    "impl_item",          # Rust impl
    "interface_declaration",  # TS/Java
})

# 跳过的目录
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build",
})

# 正则 fallback：匹配常见语言的定义语句
_SYMBOL_RE = re.compile(
    r"^[ \t]*(def|class|function|func|fn|pub fn|async fn|async def"
    r"|public|private|protected|static)\s+(\w+)",
    re.MULTILINE,
)

# 已加载的 tree-sitter Language 对象缓存（避免重复 import）
_lang_cache: dict[str, object] = {}   # ext → Language or None


def _get_language(ext: str):
    """
    按文件扩展名获取 tree-sitter Language 对象。
    未安装时返回 None，调用方降级为正则。
    """
    if ext in _lang_cache:
        return _lang_cache[ext]

    entry = _LANG_REGISTRY.get(ext)
    if entry is None:
        _lang_cache[ext] = None
        return None

    module_name, attr_name = entry
    try:
        import importlib
        from tree_sitter import Language
        mod = importlib.import_module(module_name)
        lang_fn = getattr(mod, attr_name)
        lang = Language(lang_fn())
        _lang_cache[ext] = lang
        return lang
    except Exception:
        _lang_cache[ext] = None
        return None


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    """一个提取出来的符号（函数或类定义）。"""
    name: str
    kind: str           # "function" | "class" | "method"
    line: int
    file: Path
    indent: int = 0

    @property
    def is_toplevel(self) -> bool:
        return self.indent == 0


@dataclass
class FileInfo:
    """一个文件的元信息和符号列表。"""
    path: Path
    size: int
    symbols: list[Symbol] = field(default_factory=list)

    @property
    def rel_path(self) -> str:
        return str(self.path)

    def importance_score(self) -> float:
        top_level = sum(1 for s in self.symbols if s.is_toplevel)
        size_penalty = self.size / 10_000
        return top_level - size_penalty


# ---------------------------------------------------------------------------
# RepoMap
# ---------------------------------------------------------------------------

class RepoMap:
    """
    扫描 repo，生成摘要字符串。

    用法：
        rm = RepoMap(repo_path="/path/to/repo")
        summary = rm.build(budget=8000)
    """

    def __init__(self, repo_path: str | Path) -> None:
        self._root = Path(repo_path).resolve()

    def build(self, budget: int = 8000) -> str:
        files = self._scan()
        if not files:
            return "(empty repository)"

        files.sort(key=lambda f: f.importance_score(), reverse=True)

        lines: list[str] = []
        char_count = 0
        max_chars = budget * 4

        for fi in files:
            block = self._format_file(fi)
            if char_count + len(block) > max_chars:
                remaining = len(files) - files.index(fi)
                lines.append(f"... ({remaining} more files not shown)")
                break
            lines.append(block)
            char_count += len(block)

        return "\n".join(lines)

    def _scan(self) -> list[FileInfo]:
        results: list[FileInfo] = []
        try:
            paths = sorted(self._root.rglob("*"))
        except OSError:
            return results
        for path in paths:
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size > 500_000:
                continue

            fi = FileInfo(path=path.relative_to(self._root), size=size)
            ext = path.suffix.lower()

            if ext in _LANG_REGISTRY or ext in {".py", ".js", ".ts", ".go", ".rs"}:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    fi.symbols = _extract_symbols(content, fi.path, ext)
                except OSError:
                    pass

            results.append(fi)
        return results

    def _format_file(self, fi: FileInfo) -> str:
        sym_count = len(fi.symbols)
        header = f"{fi.rel_path}"
        if sym_count:
            header += f" ({sym_count} symbol{'s' if sym_count != 1 else ''})"

        if not fi.symbols:
            return header + "\n"

        sym_lines = [header + ":"]
        for sym in fi.symbols:
            prefix = "    " if not sym.is_toplevel else "  "
            sym_lines.append(f"{prefix}{sym.kind} {sym.name} (line {sym.line})")
        return "\n".join(sym_lines) + "\n"


# ---------------------------------------------------------------------------
# 符号提取（对外暴露，供测试使用）
# ---------------------------------------------------------------------------

def _extract_symbols(content: str, filepath: Path, ext: str) -> list[Symbol]:
    """
    按扩展名选择解析方式：tree-sitter（如已安装）或正则 fallback。
    """
    lang = _get_language(ext)
    if lang is not None:
        return _extract_with_treesitter(content, filepath, lang)
    return _extract_symbols_regex(content, filepath)


def _extract_with_treesitter(content: str, filepath: Path, lang) -> list[Symbol]:
    """用 tree-sitter 提取符号，失败时降级为正则。"""
    try:
        from tree_sitter import Parser
        parser = Parser(lang)
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return _walk_tree(tree.root_node, filepath)
    except Exception:
        return _extract_symbols_regex(content, filepath)


def _walk_tree(node, filepath: Path) -> list[Symbol]:
    """递归遍历 tree-sitter AST，提取函数和类定义。"""
    results: list[Symbol] = []
    ntype = node.type

    if ntype in _FUNC_NODES and ntype != "arrow_function":
        name_node = node.child_by_field_name("name")
        if name_node:
            indent = node.start_point[1]
            kind = "method" if indent > 0 else "function"
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind=kind,
                line=node.start_point[0] + 1,
                file=filepath,
                indent=indent,
            ))
    elif ntype in _CLASS_NODES:
        name_node = node.child_by_field_name("name")
        if name_node:
            indent = node.start_point[1]
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind="class",
                line=node.start_point[0] + 1,
                file=filepath,
                indent=indent,
            ))

    for child in node.children:
        results.extend(_walk_tree(child, filepath))

    return results


# 保留原函数名供测试 import
def _extract_python_symbols(content: str, filepath: Path) -> list[Symbol]:
    """兼容旧接口，测试文件用此名调用。"""
    return _extract_symbols(content, filepath, ".py")


def _extract_symbols_regex(content: str, filepath: Path) -> list[Symbol]:
    """正则 fallback，支持多语言。"""
    symbols: list[Symbol] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        m = _SYMBOL_RE.match(line)
        if not m:
            continue
        keyword = m.group(1)
        name = m.group(2)
        # 跳过 Java/JS 修饰符误匹配（public/private 后面跟的是类型，不是名字）
        if keyword in ("public", "private", "protected", "static"):
            continue
        indent = len(line) - len(line.lstrip())
        if keyword == "class":
            kind = "class"
        elif indent > 0:
            kind = "method"
        else:
            kind = "function"
        symbols.append(Symbol(
            name=name, kind=kind, line=lineno,
            file=filepath, indent=indent,
        ))
    return symbols