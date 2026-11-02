"""Emit an accurate structural map of every source file: module docstring,
constants, classes (with methods) and functions, each with line numbers and
docstrings verbatim. Nothing here is guessed - it is read from the AST."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
OUT = []

LAYERS = [
    ("Điểm vào (entry points)", ["main.py", "load_config.py", "deploy.sh", "dev.sh"]),
    ("core/ — vòng lặp agent, chính sách, trạng thái", ["core"]),
    ("agent/ — phân tích tin nhắn, sinh câu trả lời, điều phối", ["agent"]),
    ("perception/ — đọc màn hình (DOM bridge, parser, vision)", ["perception"]),
    ("grounding/ — tìm toạ độ/element để thao tác", ["grounding"]),
    ("platforms/ — logic riêng cho TikTok (inbox, messaging, selector)", ["platforms"]),
    ("tools/ — điều khiển Chrome (Playwright/CDP)", ["tools"]),
    ("bridge/ — giao tiếp với extension Chrome", ["bridge"]),
    ("llm/ + ai/ — gọi Gemini", ["llm", "ai"]),
    ("memory/ + messaging/ — bộ nhớ hội thoại, lịch quét/nhắc", ["memory", "messaging"]),
    ("models/ — kiểu dữ liệu dùng chung", ["models"]),
    ("storage/ — PostgreSQL, catalogue sản phẩm, ledger", ["storage"]),
    ("services/ — escalation, handoff, metrics", ["services"]),
    ("control/ — control server của từng agent", ["control"]),
    ("dashboard_api/ — supervisor: quản lý tiến trình, API cho dashboard", ["dashboard_api"]),
    ("data_collection/ — thu thập dataset GUI", ["data_collection"]),
]

def doc(node):
    d = ast.get_docstring(node)
    return d.strip() if d else None

def sig(fn):
    args = []
    a = fn.args
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, dflt in zip(pos, defaults):
        s = arg.arg
        if dflt is not None:
            try: s += "=" + ast.unparse(dflt)
            except Exception: s += "=…"
        args.append(s)
    if a.vararg: args.append("*" + a.vararg.arg)
    if a.kwonlyargs:
        if not a.vararg: args.append("*")
        for arg, dflt in zip(a.kwonlyargs, a.kw_defaults):
            s = arg.arg
            if dflt is not None:
                try: s += "=" + ast.unparse(dflt)
                except Exception: s += "=…"
            args.append(s)
    if a.kwarg: args.append("**" + a.kwarg.arg)
    ret = ""
    if fn.returns:
        try: ret = " -> " + ast.unparse(fn.returns)
        except Exception: pass
    kind = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return f"{kind} {fn.name}({', '.join(args)}){ret}"

def indent(text, n=2):
    pad = " " * n
    return "\n".join(pad + l if l.strip() else l for l in text.splitlines())

def describe_py(path: Path):
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"### `{path}`\n\n(không parse được: {e})\n"
    lines = src.count("\n") + 1
    out = [f"### `{path}` — {lines} dòng\n"]
    md = doc(tree)
    if md:
        out.append("**Mục đích (module docstring):**\n")
        out.append(indent(md, 4) if False else "> " + md.replace("\n", "\n> ") + "\n")
    consts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper():
                try: val = ast.unparse(node.value)
                except Exception: val = "…"
                if len(val) > 90: val = val[:87] + "…"
                consts.append(f"- `{name}` = `{val}`  (dòng {node.lineno})")
    if consts:
        out.append("**Hằng số cấu hình:**\n"); out.extend(consts); out.append("")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            out.append(f"**class `{node.name}`{'(' + bases + ')' if bases else ''}** — dòng {node.lineno}\n")
            cd = doc(node)
            if cd: out.append("> " + cd.replace("\n", "\n> ") + "\n")
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    md_ = doc(m)
                    first = (md_.split("\n\n")[0].replace("\n", " ") if md_ else "")
                    out.append(f"- `{sig(m)}` — dòng {m.lineno}" + (f"  \n  {first}" if first else ""))
            out.append("")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"**`{sig(node)}`** — dòng {node.lineno}\n")
            fd = doc(node)
            if fd: out.append("> " + fd.replace("\n", "\n> ") + "\n")
    return "\n".join(out) + "\n"

def describe_ts(path: Path):
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.count("\n") + 1
    out = [f"### `{path}` — {lines} dòng\n"]
    # leading comment block
    m = re.match(r"\s*((?://[^\n]*\n|/\*\*?[\s\S]*?\*/\s*\n)+)", src)
    if m:
        head = re.sub(r"^\s*(//|/\*\*?|\*/|\*)\s?", "", m.group(1), flags=re.M).strip()
        if head: out.append("> " + head.replace("\n", "\n> ") + "\n")
    exports = []
    for mm in re.finditer(r"^export\s+(?:default\s+)?(?:async\s+)?(function|const|class|type|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)", src, re.M):
        ln = src[:mm.start()].count("\n") + 1
        exports.append(f"- `export {mm.group(1)} {mm.group(2)}` — dòng {ln}")
    for mm in re.finditer(r"^function\s+([A-Z][A-Za-z0-9_]*)\s*\(", src, re.M):
        ln = src[:mm.start()].count("\n") + 1
        exports.append(f"- component nội bộ `{mm.group(1)}` — dòng {ln}")
    if exports:
        out.append("**Export / component:**\n"); out.extend(exports); out.append("")
    return "\n".join(out) + "\n"

def collect():
    py = sorted(p for p in ROOT.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts and p.name != "__init__.py")
    used = set()
    for title, keys in LAYERS:
        OUT.append(f"\n## {title}\n")
        for p in py:
            rel = p.relative_to(ROOT)
            top = rel.parts[0]
            hit = (str(rel) in keys) or (top in keys and len(rel.parts) > 1)
            if hit and rel not in used and "tests" not in rel.parts:
                used.add(rel); OUT.append(describe_py(rel))
    rest = [p.relative_to(ROOT) for p in py if p.relative_to(ROOT) not in used and "tests" not in p.parts]
    if rest:
        OUT.append("\n## Khác\n")
        for rel in rest: OUT.append(describe_py(rel))
    OUT.append("\n## dashboard/src — frontend (React + TypeScript)\n")
    for p in sorted(ROOT.glob("dashboard/src/**/*.ts*")):
        if ".test." in p.name or p.suffix == ".d.ts": continue
        OUT.append(describe_ts(p.relative_to(ROOT)))
    OUT.append("\n## tests/ — bộ test Python\n")
    for p in sorted((ROOT / "tests").glob("*.py")):
        if p.name == "__init__.py": continue
        src = p.read_text(encoding="utf-8", errors="replace")
        try: md = ast.get_docstring(ast.parse(src))
        except SyntaxError: md = None
        n = src.count("\n") + 1
        cases = len(re.findall(r"^\s+(?:async )?def test_", src, re.M))
        OUT.append(f"- `{p.relative_to(ROOT)}` — {n} dòng, {cases} test" + (f"  \n  {md.split(chr(10))[0]}" if md else ""))
    OUT.append("\n## dashboard/src — test frontend (vitest)\n")
    for p in sorted(ROOT.glob("dashboard/src/**/*.test.ts*")):
        src = p.read_text(encoding="utf-8", errors="replace")
        cases = len(re.findall(r"^\s*it\(", src, re.M))
        OUT.append(f"- `{p.relative_to(ROOT)}` — {src.count(chr(10))+1} dòng, {cases} test")

collect()
print("\n".join(OUT))
