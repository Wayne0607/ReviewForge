"""Bounded Java source navigation, not a type checker or defect evidence.

Keep the declared owner when looking up common method names. Unresolved
receivers, inherited dispatch and reflection must remain unchecked; a missing
syntactic hit cannot disprove a runtime call. State pointers help an investigator
locate alternative configuration paths without deciding initialization order.
"""

from __future__ import annotations

import re
from functools import cached_property

from reviewforge.engine import symbol_extractor
from reviewforge.engine.declarations_v4 import extract_code_definitions

_NAME = r"[A-Za-z_$][\w$]*"
_PACKAGE = re.compile(rf"\bpackage\s+({_NAME}(?:\.{_NAME})*)\s*;")
_IMPORT = re.compile(rf"\bimport\s+(static\s+)?({_NAME}(?:\.{_NAME})*)\s*;")
_BINDING = re.compile(rf"\b({_NAME}(?:\.{_NAME})*(?:<[^;=()]+>)?(?:\[\])*)\s+({_NAME})\s*(?=[=;,)])")
_NOT_TYPES = frozenset({"return", "throw", "new", "case", "yield", "package", "import", "break", "continue"})


def matches_qualified(actual: str, query: str, *, package: str) -> bool:
    """A simple owner query may match multiple packages, never a nested owner."""
    if actual == query:
        return True
    # Package omission is permitted only at the package/class boundary.
    # Dropping arbitrary prefixes would make Gate.method match Nested.Gate.method.
    return bool(package) and actual.removeprefix(package + ".") == query


class JavaSource:
    def __init__(self, source: str, path: str, definitions=None):
        self.source = source
        self.path = path
        self.mask = symbol_extractor.mask_non_code(source, "java")
        self.definitions = list(definitions) if definitions is not None else extract_code_definitions(source, path)
        self.classes = [d for d in self.definitions if d.symbol_type == "class" and d.end_line >= d.line > 0]
        self.functions = [d for d in self.definitions if d.symbol_type == "function" and d.end_line >= d.line > 0]
        package = _PACKAGE.search(self.mask)
        self.package = package.group(1) if package else ""
        self.imports = {}
        self.static_imports = {}
        for match in _IMPORT.finditer(self.mask):
            imported = match.group(2)
            table = self.static_imports if match.group(1) else self.imports
            table.setdefault(imported.rsplit(".", 1)[-1], set()).add(imported)

    @cached_property
    def calls(self):
        return symbol_extractor.extract_calls(self.source, self.path)

    def owner(self, line: int):
        containing = [d for d in self.classes if d.line <= line <= d.end_line]
        return min(containing, key=lambda d: d.end_line - d.line, default=None)

    def class_name(self, owner) -> str:
        if owner is None:
            return ""
        chain = [d for d in self.classes if d.line <= owner.line and owner.end_line <= d.end_line]
        chain.sort(key=lambda d: (d.line, -d.end_line))
        return ".".join(filter(None, [self.package, *(d.name for d in chain)]))

    def qualified_definition(self, definition) -> str:
        if definition.symbol_type == "class":
            return self.class_name(definition)
        return ".".join(filter(None, [self.class_name(self.owner(definition.line)), definition.name]))

    def definition_at(self, name: str, line: int):
        containing = [d for d in self.definitions if d.name == name and (d.start_line or d.line) <= line <= d.end_line]
        return min(containing, key=lambda d: d.end_line - d.line, default=None)

    def _function(self, line: int):
        return min(
            (d for d in self.functions if d.line <= line <= d.end_line),
            key=lambda d: d.end_line - d.line,
            default=None,
        )

    @cached_property
    def bindings(self):
        return [
            (match.group(2), match.group(1), self.mask[: match.start()].count("\n") + 1)
            for match in _BINDING.finditer(self.mask)
            if match.group(1) not in _NOT_TYPES
        ]

    def _receiver_type(self, receiver: str, line: int) -> tuple[bool, str | None]:
        name = receiver.removeprefix("this.")
        if not re.fullmatch(_NAME, name):
            return False, None
        method, owner = self._function(line), self.owner(line)
        locals_ = []
        fields = []
        for binding, type_, declaration in self.bindings:
            if binding != name or self.owner(declaration) != owner:
                continue
            scope = self._function(declaration)
            if scope == method and declaration <= line and not receiver.startswith("this."):
                locals_.append(type_)
            elif scope is None:
                fields.append(type_)
        choices = locals_ or fields
        if len(choices) != 1:
            return bool(choices), None  # Ambiguous/shadowed bindings remain unchecked.
        return True, choices[0] if choices[0] != "var" else None

    def _type_name(self, name: str) -> str | None:
        name = re.sub(r"<.*>|\[\]", "", name).strip()
        imported = self.imports.get(name, set())
        if len(imported) == 1:
            return next(iter(imported))
        if imported:
            return None
        local = [d for d in self.classes if d.name == name]
        if len(local) == 1:
            return self.class_name(local[0])
        if "." in name and re.fullmatch(rf"{_NAME}(?:\.{_NAME})+", name):
            return name
        # Same-package receiver names are navigation candidates, not proof
        # of runtime type. Unknown lower-case variables never become classes.
        if name[:1].isupper():
            return ".".join(filter(None, [self.package, name]))
        return None

    def call_target(self, call) -> str | None:
        def value(name, default=""):
            return call.get(name, default) if isinstance(call, dict) else getattr(call, name, default)

        callee = str(value("callee"))
        receiver = str(value("receiver"))
        line = int(value("line", 0) or 0)
        if "." in callee and not receiver:
            receiver, callee = callee.rsplit(".", 1)
        owner = self.owner(line)
        if receiver == "this":
            return ".".join(filter(None, [self.class_name(owner), callee])) if owner else None
        if receiver == "super":
            return None
        if receiver:
            bound, receiver_type = self._receiver_type(receiver, line)
            # File-wide receiver_type hints may belong to a sibling method.
            target = self._type_name(receiver_type) if receiver_type else None
            if not bound:
                target = self._type_name(receiver)
            return f"{target}.{callee}" if target else None
        local = [d for d in self.functions if d.name == callee and self.owner(d.line) == owner]
        if local and owner:
            return f"{self.class_name(owner)}.{callee}"
        imported = self.static_imports.get(callee, set())
        if len(imported) == 1:
            return next(iter(imported))
        return self._type_name(callee) if callee[:1].isupper() else None

    def state_navigation(self, definition, *, max_chars: int = 600) -> str:
        """Point to mutable static fields and same-class reference locations.

        Inspect the callee and one layer of local helpers. Do not infer writers,
        execution order, or completeness from this lexical index.
        """
        owner = self.owner(definition.line)
        if owner is None or definition.symbol_type != "function":
            return ""
        lines = self.mask.splitlines()
        fields = {}
        for number in range(owner.line, owner.end_line + 1):
            if self.owner(number) != owner or any(d.line <= number <= d.end_line for d in self.functions):
                continue
            line = lines[number - 1]
            if not re.search(r"\bstatic\b", line) or re.search(r"\bfinal\b", line):
                continue
            match = re.search(rf"\b({_NAME})\s*(?:=|;)\s*", line)
            if match:
                fields[match.group(1)] = number
        if not fields:
            return ""
        methods = [d for d in self.functions if self.owner(d.line) == owner]
        related = [definition]
        for call in self.calls:
            if not definition.line <= call.line <= definition.end_line:
                continue
            target = self.call_target(call)
            related.extend(d for d in methods if self.qualified_definition(d) == target)
        used = set(re.findall(_NAME, "\n".join("\n".join(lines[d.line - 1 : d.end_line]) for d in related)))
        entries = []
        for name, declaration_line in fields.items():
            if name not in used:
                continue
            matcher = re.compile(rf"\b{re.escape(name)}\b")
            references = []
            for method in methods:
                for number in range(method.line, method.end_line + 1):
                    if self.owner(number) == owner and matcher.search(lines[number - 1]):
                        references.append(f"{method.name}@{method.line} (reference {number})")
                        break
            entries.append(
                f"{name}@{declaration_line}: "
                + ", ".join(references[:6])
                + ("; more omitted" if len(references) > 6 else "")
            )
        if not entries:
            return ""
        text = "State navigation (not evidence; inspect config/reset callers; order unproved): " + "; ".join(
            entries[:4]
        )
        if len(entries) > 4:
            text += "; more fields omitted"
        marker = " ... omitted"
        return text if len(text) <= max_chars else (text[: max(0, max_chars - len(marker))] + marker)[:max_chars]
