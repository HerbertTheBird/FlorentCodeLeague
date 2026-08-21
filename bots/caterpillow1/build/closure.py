"""Which module-level names does the runtime actually need?

`chip_precompute.py` is the offline solver: it builds the chip tables, and it is
796 lines long. The bot uses five names out of it. Shipping the rest to the
platform is dead weight, and hand-copying the five is how the copy silently
drifts from the original.

So the runtime module is GENERATED: name the roots, take the transitive closure
of module-level definitions they reference, and emit exactly that. The solver
stays the single source of truth and nothing has to be kept in sync by hand.
"""

from __future__ import annotations

import ast


def closure(source: str, roots: set[str]) -> tuple[list[ast.stmt], set[str]]:
    """(statements to emit, in original order; imports they need).

    A module-level `def`, `class` or assignment is a "definition". Starting from
    `roots`, repeatedly pull in any definition whose name is referenced by
    something already pulled in, until nothing new appears.
    """
    tree = ast.parse(source)
    defs: dict[str, ast.stmt] = {}
    order: list[tuple[int, ast.stmt]] = []
    imports: list[ast.stmt] = []

    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            continue
        names = _defined_names(node)
        if not names:
            continue
        for name in names:
            defs[name] = node
        order.append((index, node))

    wanted: set[str] = set()
    frontier = set(roots)
    while frontier:
        name = frontier.pop()
        node = defs.get(name)
        if node is None or name in wanted:
            continue
        wanted.add(name)
        for referenced in _referenced_names(node):
            if referenced in defs and referenced not in wanted:
                frontier.add(referenced)

    missing = roots - set(defs)
    if missing:
        raise SystemExit(f"roots not defined at module level: {sorted(missing)}")

    keep = [node for _i, node in order
            if wanted & set(_defined_names(node))]
    used_imports = _needed_imports(imports, keep)
    return keep, used_imports


def _defined_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def _referenced_names(node: ast.stmt) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _needed_imports(imports: list[ast.stmt], keep: list[ast.stmt]) -> list[ast.stmt]:
    referenced = set()
    for node in keep:
        referenced |= _referenced_names(node)
        referenced |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                referenced.add(n.value.id)
    out = []
    for node in imports:
        bound = {(a.asname or a.name).split(".")[0] for a in node.names}
        if bound & referenced:
            out.append(node)
    return out
