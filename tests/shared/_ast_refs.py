"""Every name a module reaches, resolved to the module it came from — ONE
resolver, shared by the AST gates that ask "who uses X".

Two gates need it: `test_rewrite_layer_is_gone.py` (no script or test uses the
deleted PyYAML rewrite layer, whatever module it is reached through) and
`test_yaml_reader_boundary.py` (who reads YAML with PyYAML, ADR-036 §12 — there
the module matters: `yaml.load` is a parser, `json.load` is not).

`refs(tree)` yields one `Ref` per place a name is reached, under whatever local
spelling, reported by the CANONICAL name:

* imported (`from yaml import safe_load as sl`) — the import itself is a
  `Ref` with `via_import=True` (and `local="sl"`), and every later `sl` is a
  `Ref` to (`yaml`, `safe_load`);
* reached as an attribute (`import yaml as y` … `y.safe_load`;
  `import _lib_io as io` … `io.load_yaml_file`; `tools._lib_io.load_yaml_file`);
* fetched by a constant string (`getattr(yaml, "safe_load")`, `hasattr`);
* referred to by its bare name (a module using what it defines, or a builtin):
  `module` is `None`.

A `Ref` is any reference, not only a call: `f = yaml.safe_load` and
`map(yaml.safe_load, …)` reach the parser as surely as `yaml.safe_load(…)`.

`module` is the dotted module path as the file spells it (`yaml`,
`yaml.loader`, `tools._lib_io`, `scripts.tools._lib_io`; a relative
`from ._lib_io import x` is `_lib_io`), or `None` when the reference does not
resolve to an import (an attribute of a local object, a bare name). The
caller decides what counts as the same module (`module_is`).

`scope` is the qualname of the innermost enclosing `def` / `class`
(`Outer.method.inner`), or `"<module>"`. A class's bases and keywords, and a
function's decorators and defaults, belong to that class / function: the
class built on `yaml.SafeLoader` is the reader, not the module around it.
`top` is the top-level binding the reference helps define: the outermost
`def` / `class` name, or the target of a module-level assignment
(`SAFE_LOADER = yaml.CSafeLoader`); `None` for other module-level code.

A star import (`from yaml import *`) cannot say which names it binds, so a
bare name that no other import binds carries the star-imported modules in
`star`; the caller decides whether the name is one of theirs. A plain
rebinding of a module (`Y = yaml`, `x = io`, at module level or in a function)
is followed: `Y.safe_load` is (`yaml`, `safe_load`).

⚠️ It reads names, not values: a name built at run time
(`getattr(m, "safe_" + "load")`, `importlib.import_module`, `__import__`), a
module reached through a mapping (`yaml.__dict__["safe_load"]`,
`sys.modules["yaml"]`), or a module or parser handed in as an argument (an
attribute of a parameter) is not a `Ref` to it.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class Ref:
    module: Optional[str]
    name: str
    scope: str
    top: Optional[str]
    via_import: bool
    lineno: int
    #: for an import (`via_import`), the name it binds here
    local: Optional[str] = None
    #: for a bare name no import binds: the modules star-imported in this file
    star: "tuple[str, ...]" = ()


def module_is(module: Optional[str], *, last: str = "", package: str = "") -> bool:
    """Whether *module* is the module whose last component is *last*
    (`tools._lib_io` is `_lib_io`), or *package* or one of its submodules
    (`yaml.loader` is in `yaml`)."""
    if not module:
        return False
    if last and module.rsplit(".", 1)[-1] == last:
        return True
    return bool(package) and (module == package or module.startswith(package + "."))


def _bindings(tree: ast.AST
              ) -> "tuple[dict[str, str], dict[str, tuple[str, str]], tuple[str, ...]]":
    """(local name -> module), (local name -> (module, canonical name)) and the
    star-imported modules, from every import in the file, at any depth (a lazy
    `import yaml` inside a function binds the same module), then every plain
    rebinding of a module (`Y = y`)."""
    modules: "dict[str, str]" = {}
    names: "dict[str, tuple[str, str]]" = {}
    stars: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    modules[a.asname] = a.name
                else:
                    # `import a.b.c` binds `a`; the chain is resolved per use
                    modules.setdefault(a.name.split(".")[0], a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            src = node.module
            for a in node.names:
                local = a.asname or a.name
                if a.name == "*":
                    if src is not None:
                        stars.append(src)
                    continue
                if src is None:            # `from . import _lib_io`
                    modules[local] = a.name
                else:
                    # `from tools import _lib_io as io` binds a module or a
                    # name; it is recorded as both and the use decides
                    names[local] = (src, a.name)
                    modules[local] = f"{src}.{a.name}"
    rebinds = [(n.targets[0].id, n.value.id) for n in ast.walk(tree)
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Name)]
    # `Y = y; Z = Y` — follow chains, one pass per rebinding at most (a name
    # rebound to two different modules in two functions must not oscillate;
    # the file is read flow-insensitively, so the last rebinding wins)
    for _ in range(len(rebinds)):
        before = dict(modules)
        for target, source in rebinds:
            if source in modules:
                modules[target] = modules[source]
        if modules == before:
            break
    return modules, names, tuple(stars)


def _dotted(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


class _Walker(ast.NodeVisitor):
    def __init__(self, modules: "dict[str, str]", names: "dict[str, tuple[str, str]]",
                 stars: "tuple[str, ...]" = ()):
        self.modules, self.names, self.stars = modules, names, stars
        self.stack: "list[str]" = []
        self.top: Optional[str] = None
        self.out: "list[Ref]" = []

    def _scope(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _emit(self, module: Optional[str], name: str, node: ast.AST,
              via_import: bool = False, local: Optional[str] = None,
              star: "tuple[str, ...]" = ()) -> None:
        self.out.append(Ref(module, name, self._scope(), self.top, via_import,
                            getattr(node, "lineno", 0), local, star))

    def _module_of(self, value: ast.AST) -> Optional[str]:
        """The module *value* names, if it names one through an import:
        `y` (bound by `import yaml as y`), `tools._lib_io` (`import tools._lib_io`)."""
        if isinstance(value, ast.Name):
            return self.modules.get(value.id)
        dotted = _dotted(value)
        if dotted and "." in dotted:
            head, rest = dotted.split(".", 1)
            if head in self.modules:
                return f"{self.modules[head]}.{rest}"
        return None

    # scopes ---------------------------------------------------------------
    def _enter(self, node: ast.AST, name: str) -> None:
        outer_top = self.top
        if not self.stack:
            self.top = name
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()
        self.top = outer_top

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def _assign(self, node: ast.AST, targets: "list[ast.AST]") -> None:
        if self.stack:
            self.generic_visit(node)
            return
        bound = [t.id for t in targets if isinstance(t, ast.Name)]
        outer_top = self.top
        self.top = bound[0] if bound else None
        self.generic_visit(node)
        self.top = outer_top

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node, node.targets)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign(node, [node.target])

    # references -----------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        src = node.module                   # None for `from . import x`
        for a in node.names:
            self._emit(src, a.name, node, via_import=True, local=a.asname or a.name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if node.id in self.names:
                self._emit(*self.names[node.id], node)
            else:
                self._emit(None, node.id, node, star=self.stars)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._emit(self._module_of(node.value), node.attr, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            self._emit(self._module_of(node.args[0]), node.args[1].value, node)
        self.generic_visit(node)


def refs(tree: ast.AST) -> "Iterator[Ref]":
    """Every reference in *tree*, in source order (see the module docstring)."""
    w = _Walker(*_bindings(tree))
    w.visit(tree)
    return iter(w.out)
