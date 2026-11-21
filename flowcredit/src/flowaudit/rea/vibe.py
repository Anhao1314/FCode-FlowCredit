"""Bounded source-quality scan for AI-generated code patterns.

Vibe coding produces specific, detectable quality patterns that differ
from human-written code:

* god files / god functions (agents generate long monolithic code)
* TODO/FIXME density (agents leave markers they never follow up on)
* silent exception swallowing (bare except/empty catch — agents often
  suppress errors to make tests pass)
* dead imports (agents import modules they end up not using)
* repeated inline literals (agents hardcode the same value in many places)
* copy-paste divergence (the same function body appears in several files)
* scaffold leftovers (pass/NotImplementedError stubs, AI chat residue in
  comments/strings)
* conflicting constants (same UPPER_CASE name, different literal value)

These are **quality facts**, not security findings. They feed the VIBE
rule categories in the rule engine; the host oracle decides severity.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["scan_code_quality"]

SOURCE_SUFFIXES = {".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", "vendor", "work"}
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "test-", "/tests/", "/test/")
MAX_FILE_LINES = 1200
GOD_FILE_LINES = 500
GOD_FUNCTION_LINES = 80
MAX_TODO_DENSITY = 0.02  # 2% of lines
#: 圈复杂度上限（McCabe 口径：判定点计数；>15 属结构纠缠信号）。
MAX_COMPLEXITY = 15
#: 事实行封顶（与 MAX_QUALITY_ROWS 同量级，避免事实文件爆炸）。
MAX_CYCLE_ROWS = 20
MAX_COMPLEX_ROWS = 30
MAX_SECRET_ROWS = 20
MAX_DEP_ROWS = 30

#: VIBE-07 安全卫生:硬编码密钥的强信号模式（宁可漏、不可吵）。
_SECRET_PATTERNS = (
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("generic-secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|credential)\b\s*[:=]\s*"
        r"[\"'][A-Za-z0-9_\-+/=]{20,}[\"']")),
)
#: 供应链卫生:这些 spec 等于没有上限。
_UNPINNED_SPECS = {"", "*", "latest", "x", ">=*"}
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 2000
MAX_TOTAL_BYTES = 64 * 1024 * 1024

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|WORKAROUND)\b", re.IGNORECASE)
_SILENT_CATCH_RE = re.compile(
    r"(?:except\s*(?:Exception)?\s*(?:as\s+\w+)?\s*:\s*\n\s+pass"
    r"|except\s*\w*\s*:\s*pass"
    r"|catch\s*\([^)]*\)\s*\{\s*\}"
    r"|catch\s*\{)"
)
_MAGIC_NUMBER_RE = re.compile(r"(?<![.\w])(\d{3,})(?![.\w])")
_IMPORT_RE = re.compile(
    r"^(?:import\s+(\S+)|from\s+(\S+)\s+import)", re.MULTILINE
)

# AI 指纹：对话残留（模型把"聊天口吻"写进注释/字符串）
_AI_RESIDUE_PHRASES = (
    "as an ai",
    "ai language model",
    "as a language model",
    "hope this helps",
    "希望对你有所帮助",
    "希望这对你有帮助",
    "当然，以下是",
    "好的，以下是",
)
MAX_QUALITY_ROWS = 50
DUP_MIN_BODY_LINES = 6          # 小于这个函数体的重复视为样板噪音
_STUB_STATEMENTS = ("pass", "...", "raise NotImplementedError")


def _comment_lines(relative: str, path: Path, lines: list[str]):
    """Yield (line_no, comment_text) for Python comments and JS // + /* */.

    Only real comments — string literals are excluded so a phrase table
    (like this module's own) never matches itself.
    """
    suffix = path.suffix.lower()
    if suffix == ".py":
        import io
        import tokenize
        try:
            source = "\n".join(lines)
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type == tokenize.COMMENT:
                    yield tok.start[0], tok.string
        except (tokenize.TokenError, IndentationError, OSError):
            return
        return
    if suffix in {".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}:
        in_block = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if in_block:
                yield i, stripped
                if "*/" in stripped:
                    in_block = False
                continue
            if "/*" in stripped:
                yield i, stripped[stripped.index("/*"):]
                in_block = "*/" not in stripped[stripped.index("/*") + 2:]
                continue
            idx = stripped.find("//")
            if idx >= 0:
                yield i, stripped[idx:]
        return


def _is_test(relative: str) -> bool:
    lowered = relative.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def _walk_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                return files
            files.append(path)
    return files


def _scan_python(path: Path, text: str, lines: list[str],
                 rel: str) -> dict[str, Any]:
    """Python-specific analysis using the ast module."""
    facts: dict[str, Any] = {}
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        facts["parse_error"] = True
        return facts

    # 模块级常量（供「同名常量取值分歧」聚合）
    constants = []
    for node in tree.body:
        target = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value_node = node.target, node.value
        if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) >= 2:
            if isinstance(value_node, ast.Constant) and isinstance(
                    value_node.value, (int, float, str, bool)):
                constants.append({
                    "name": target.id, "line": node.lineno,
                    "value": repr(value_node.value),
                })
    if constants:
        facts["_constants"] = constants

    # 函数体签名（供「重复实现 / 占位桩」聚合）
    functions = []
    stubs = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [stmt for stmt in node.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str))]
        stub = False
        if len(body) == 1:
            stmt = body[0]
            stub = (
                isinstance(stmt, ast.Pass)
                or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is Ellipsis)
                or (isinstance(stmt, ast.Raise)
                    and isinstance(stmt.exc, ast.Call)
                    and getattr(stmt.exc.func, "id", "") == "NotImplementedError")
                or (isinstance(stmt, ast.Raise)
                    and getattr(stmt.exc, "id", "") == "NotImplementedError")
            )
        if stub and not node.name.startswith("__"):
            stubs.append({"name": node.name, "line": node.lineno})
            continue
        length = (node.end_lineno or node.lineno) - node.lineno
        if length >= DUP_MIN_BODY_LINES and not node.name.startswith("__"):
            segment = ast.get_source_segment(text, node) or ""
            normalized = " ".join(segment.split())
            functions.append({
                "name": node.name, "line": node.lineno, "lines": length,
                "body_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            })
    if functions:
        facts["_functions"] = functions
    if stubs:
        facts["_stubs"] = stubs

    # God functions
    god_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno
            if length > GOD_FUNCTION_LINES:
                god_functions.append({
                    "name": node.name, "line": node.lineno, "lines": length,
                })
    if god_functions:
        facts["god_functions"] = god_functions

    # Dead imports (imported but never referenced by name in the rest of the file)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports.add(name)
    used_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attr = node
            while isinstance(attr, ast.Attribute):
                attr = attr.value
            if isinstance(attr, ast.Name):
                used_names.add(attr.id)
    dead_imports = sorted(imports - used_names - {"*"})
    if dead_imports:
        facts["dead_imports"] = dead_imports

    return facts


def _scan_generic(text: str, lines: list[str], rel: str) -> dict[str, Any]:
    """Language-agnostic scans (regex-based)."""
    facts: dict[str, Any] = {}

    # Silent catch
    catches = []
    for i, line in enumerate(lines, start=1):
        if _SILENT_CATCH_RE.search(line):
            catches.append({"file": rel, "line": i})
            if len(catches) >= 20:
                break
    if catches:
        facts["silent_catches"] = catches

    # Repeated magic numbers (same number >= 3 times, excluding test files)
    counter: dict[str, int] = {}
    for line in lines:
        for m in _MAGIC_NUMBER_RE.finditer(line):
            counter[m.group(1)] = counter.get(m.group(1), 0) + 1
    hot = [(num, count) for num, count in counter.items() if count >= 5]
    hot.sort(key=lambda x: -x[1])
    if hot:
        facts["repeated_literals"] = [
            {"value": num, "count": count} for num, count in hot[:10]
        ]

    return facts


def _module_name_for(rel: str) -> str:
    """文件相对路径 → 点分模块名（src/a/b.py → src.a.b；__init__ 归包名）。"""
    parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def _resolve_relative(module_name: str, level: int, target: str | None) -> str | None:
    """相对导入（from ..x import y）→ 绝对模块名；越界返回 None。"""
    if level <= 0:
        return target
    parts = module_name.split(".")
    # module_name 是"当前文件"：level=1 指同包（父一级包），level=2 再退一级。
    base = parts[:-1]
    for _ in range(level - 1):
        if not base:
            return None
        base = base[:-1]
    if target:
        base = base + target.split(".")
    return ".".join(base) if base else None


def _python_import_cycles(root: Path) -> list[dict[str, Any]]:
    """Python 模块强连通分量 → import 环事实（VIBE-06 结构纠缠）。

    只解析静态 import；动态 __import__ / importlib 不追（诚实标注在规则
    语义里）。边按文件路径，环内按路径排序后去重。
    """
    import ast

    files: list[str] = []
    for path in _walk_source_files(root):
        rel = str(path.relative_to(root))
        if rel.endswith(".py"):
            files.append(rel)
    file_set = set(files)
    module_to_rel = {_module_name_for(rel): rel for rel in files}
    edges: dict[str, set[str]] = {}
    edge_lines: dict[tuple[str, str], int] = {}
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        module_name = _module_name_for(rel)
        for node in ast.walk(tree):
            targets: list[tuple[str | None, int]] = []
            if isinstance(node, ast.Import):
                targets = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                package = _resolve_relative(module_name, node.level or 0, node.module)
                targets = [(package, node.lineno)]
                for alias in node.names:
                    # from . import y / from pkg import y：y 可能是子模块
                    candidate = f"{package}.{alias.name}" if package else alias.name
                    targets.append((candidate, node.lineno))
            for target, line in targets:
                if not target:
                    continue
                dep_rel = module_to_rel.get(target)
                if dep_rel is None or dep_rel == rel or dep_rel not in file_set:
                    continue
                edges.setdefault(rel, set()).add(dep_rel)
                edge_lines.setdefault((rel, dep_rel), line)
    # Tarjan（迭代实现，避免深递归）
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = [0]
    nodes = [rel for rel in files if rel in edges or any(rel in deps for deps in edges.values())]
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) >= 2:
                    sccs.append(sorted(component))
    sccs.sort(key=lambda members: (len(members), members))
    cycles: list[dict[str, Any]] = []
    for members in sccs[:MAX_CYCLE_ROWS]:
        member_set = set(members)
        sample_edges = [
            {"from": src, "to": dst, "line": edge_lines.get((src, dst), 0)}
            for src in members for dst in sorted(edges.get(src, ()))
            if dst in member_set
        ][:10]
        cycles.append({"cycle": members, "size": len(members), "edges": sample_edges})
    return cycles


class _ComplexityVisitor:
    """McCabe 口径的判定点计数（不下降进嵌套 def/class——各自单独计）。"""

    COUNT_NODES = (
        "If", "For", "AsyncFor", "While", "ExceptHandler", "IfExp", "Match",
    )

    def __init__(self) -> None:
        import ast

        self.complexity = 1
        self._ast = ast

    def visit(self, node: Any) -> None:
        ast = self._ast
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if type(child).__name__ in self.COUNT_NODES:
                self.complexity += 1
            if isinstance(child, ast.BoolOp):
                self.complexity += max(0, len(child.values) - 1)
            self.visit(child)


def _complex_functions(root: Path) -> list[dict[str, Any]]:
    """生产代码里圈复杂度超过阈值的函数（VIBE-01 家族的结构复杂度事实）。"""
    import ast

    rows: list[dict[str, Any]] = []
    for path in _walk_source_files(root):
        rel = str(path.relative_to(root))
        if not rel.endswith(".py") or _is_test(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            visitor = _ComplexityVisitor()
            visitor.visit(node)
            if visitor.complexity > MAX_COMPLEXITY:
                rows.append({
                    "path": rel,
                    "function": node.name,
                    "line": node.lineno,
                    "complexity": visitor.complexity,
                })
    rows.sort(key=lambda row: (-row["complexity"], row["path"], row["line"]))
    return rows[:MAX_COMPLEX_ROWS]


def _mask_secret(value: str) -> str:
    """证据只留前缀与长度——bundle 不能成为二次泄漏源。"""
    value = str(value)
    return f"{value[:4]}…({len(value)} chars)"


def _scan_secrets(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _walk_source_files(root):
        rel = str(path.relative_to(root))
        if _is_test(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, pattern in _SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    rows.append({"path": rel, "line": lineno, "kind": kind,
                                 "masked": _mask_secret(match.group(0))})
                    break
            if len(rows) >= MAX_SECRET_ROWS:
                return rows
    return rows


def _walk_manifests(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name == "package.json" or (name.startswith("requirements") and name.endswith(".txt")):
                out.append(Path(dirpath) / name)
    return out


def _scan_unpinned_dependencies(root: Path) -> list[dict[str, Any]]:
    """供应链卫生:requirements 没有 == 固定、package.json 用 */latest/>=。"""
    import json as _json

    rows: list[dict[str, Any]] = []
    for path in _walk_manifests(root):
        rel = str(path.relative_to(root))
        if _is_test(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.name == "package.json":
            try:
                data = _json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                deps = data.get(section)
                if not isinstance(deps, dict):
                    continue
                for name, spec in sorted(deps.items()):
                    spec_text = str(spec).strip()
                    if spec_text in _UNPINNED_SPECS or spec_text.startswith(">"):
                        rows.append({"path": rel, "package": str(name),
                                     "spec": spec_text or "(empty)", "kind": "package-json"})
        else:
            for lineno, raw in enumerate(text.splitlines(), 1):
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                if "==" in line:
                    continue
                package = re.split(r"[<>=!~;\[\s]", line)[0]
                if not package:
                    continue
                rows.append({"path": rel, "line": lineno, "package": package,
                             "spec": line, "kind": "requirements"})
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (row["path"], row["package"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= MAX_DEP_ROWS:
            break
    return unique


def scan_code_quality(root: Path) -> dict[str, Any]:
    """Scan a source tree for AI-generated code quality patterns.

    Returns bounded facts; the rule engine turns them into candidates,
    and the host oracle decides which are findings.
    """
    root = Path(root)
    if not root.is_dir():
        return {"status": "not_a_directory"}

    findings_by_file: list[dict[str, Any]] = []
    god_files: list[dict[str, Any]] = []
    todo_hotspots: list[dict[str, Any]] = []
    literal_sprawl: list[dict[str, Any]] = []
    constant_defs: dict[str, list[dict[str, Any]]] = {}
    function_defs: dict[str, list[dict[str, Any]]] = {}
    placeholder_stubs: list[dict[str, Any]] = []
    ai_residue: list[dict[str, Any]] = []
    total_files = 0
    total_lines = 0
    truncated = False

    for path in _walk_source_files(root):
        relative = str(path.relative_to(root))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            truncated = True
            continue
        try:
            text = path.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        line_count = len(lines)
        total_files += 1
        total_lines += line_count

        entry: dict[str, Any] = {"path": relative, "lines": line_count,
                                 "bytes": size, "test": _is_test(relative)}

        # God file (skip tests)
        if not _is_test(relative) and line_count > GOD_FILE_LINES:
            god_files.append({"path": relative, "lines": line_count})

        # TODO/FIXME density (skip tests)
        if not _is_test(relative):
            todos = [(i, line.strip()[:200]) for i, line in enumerate(lines, 1)
                     if _TODO_RE.search(line)]
            density = len(todos) / max(line_count, 1)
            if todos and density > MAX_TODO_DENSITY:
                todo_hotspots.append({
                    "path": relative, "count": len(todos),
                    "density": round(density, 4),
                    "lines": [t[0] for t in todos[:10]],
                    "samples": [t[1] for t in todos[:5]],
                })

        # AI 对话残留：只在注释里找（全语言，非测试文件）。
        # 限定注释而非字符串：短语表本身是字符串常量，扫字符串会自我命中。
        if not _is_test(relative):
            for i, comment in _comment_lines(relative, path, lines):
                lowered = comment.lower()
                phrase = next((p for p in _AI_RESIDUE_PHRASES if p in lowered), None)
                if phrase:
                    ai_residue.append({"path": relative, "line": i, "phrase": phrase})
                    if len(ai_residue) >= MAX_QUALITY_ROWS:
                        break

        # Language-specific analysis
        file_facts: dict[str, Any] = {}
        if path.suffix == ".py":
            file_facts = _scan_python(path, text, lines, relative)
        file_facts.update(_scan_generic(text, lines, relative))

        # 聚合模块级信号（不留在每文件 issues 里，规则看顶层列表）
        for const in file_facts.pop("_constants", []):
            constant_defs.setdefault(const["name"], []).append({
                "path": relative, "line": const["line"], "value": const["value"],
            })
        for fn in file_facts.pop("_functions", []):
            if _is_test(relative):
                continue
            function_defs.setdefault(fn["name"], []).append({
                "path": relative, "line": fn["line"], "lines": fn["lines"],
                "body_hash": fn["body_hash"],
            })
        for stub in file_facts.pop("_stubs", []):
            if _is_test(relative):
                continue
            placeholder_stubs.append({"path": relative, "line": stub["line"],
                                      "name": stub["name"]})

        # 魔法值散落：同文件同一字面量 >=5 次
        for item in file_facts.get("repeated_literals") or []:
            literal_sprawl.append({"path": relative, "value": item.get("value"),
                                   "count": item.get("count")})

        if file_facts:
            entry["issues"] = file_facts
            findings_by_file.append(entry)

    # 同名常量取值分歧：跨文件、同名、不同字面量 —— 两处真相的强信号
    constant_conflicts: list[dict[str, Any]] = []
    for name, definitions in constant_defs.items():
        if name.startswith("_"):
            continue  # 私有常量＝模块内默认值，跨文件分歧属正常
        if len({d["value"] for d in definitions}) > 1 and \
                len({d["path"] for d in definitions}) > 1:
            constant_conflicts.append({
                "name": name,
                "definitions": definitions[:10],
            })
    constant_conflicts.sort(key=lambda row: row["name"])

    # 重复实现：同名函数体哈希跨文件一致（≥6 行）—— 复制粘贴分叉
    duplicate_functions: list[dict[str, Any]] = []
    for name, defs in function_defs.items():
        by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in defs:
            by_hash.setdefault(item["body_hash"], []).append(item)
        for group in by_hash.values():
            if len(group) >= 2 and len({g["path"] for g in group}) >= 2:
                duplicate_functions.append({
                    "name": name,
                    "lines": group[0]["lines"],
                    "files": [{"path": g["path"], "line": g["line"]} for g in group[:6]],
                })
    duplicate_functions.sort(key=lambda row: row["name"])

    return {
        "status": "observed",
        "total_files": total_files,
        "total_lines": total_lines,
        "god_files": god_files,
        "todo_hotspots": todo_hotspots,
        "literal_sprawl": literal_sprawl[:MAX_QUALITY_ROWS],
        "constant_conflicts": constant_conflicts[:MAX_QUALITY_ROWS],
        "duplicate_functions": duplicate_functions[:MAX_QUALITY_ROWS],
        "placeholder_stubs": placeholder_stubs[:MAX_QUALITY_ROWS],
        "ai_residue": ai_residue[:MAX_QUALITY_ROWS],
        "import_cycles": _python_import_cycles(root),
        "complex_functions": _complex_functions(root),
        "secrets_in_source": _scan_secrets(root),
        "unpinned_dependencies": _scan_unpinned_dependencies(root),
        "issue_files": findings_by_file,
        "truncated": truncated,
        "thresholds": {
            "god_file_lines": GOD_FILE_LINES,
            "god_function_lines": GOD_FUNCTION_LINES,
            "max_todo_density": MAX_TODO_DENSITY,
            "max_complexity": MAX_COMPLEXITY,
        },
    }