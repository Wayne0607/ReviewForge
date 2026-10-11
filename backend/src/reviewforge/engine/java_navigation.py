"""Bounded Java source navigation, not a type checker or defect evidence.

Keep the declared owner when looking up common method names. Unresolved
receivers, inherited dispatch and reflection must remain unchecked; a missing
syntactic hit cannot disprove a runtime call. State pointers help an investigator
locate alternative configuration paths without deciding initialization order.
Explicit superclass member declarations can guide Context without proving dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class JavaField:
    name: str
    line: int
    owner: symbol_extractor.SymbolInfo
    mutable_static: bool
    type_name: str = ""
    access: str = "package"


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
            (d for d in self.functions if (d.start_line or d.line) <= line <= d.end_line),
            key=lambda d: d.end_line - d.line,
            default=None,
        )

    @cached_property
    def field_declarations(self) -> tuple[JavaField, ...]:
        """Declared members in a recognized class body, never local statements.

        The lexical brace depth excludes initializer blocks and anonymous
        bodies too. Unsupported/inherited declarations remain unresolved.
        """
        fields = []
        for match in _BINDING.finditer(self.mask):
            if match.group(1) in _NOT_TYPES:
                continue
            offset = match.start()
            line = self.mask.count("\n", 0, offset) + 1
            owner = self.owner(line)
            if owner is None or self._function(line) is not None:
                continue
            class_start = sum(len(row) for row in self.mask.splitlines(keepends=True)[: owner.line - 1])
            body = self.mask.find("{", class_start)
            if body < 0 or body >= offset:
                continue
            depth = self.mask.count("{", 0, offset) - self.mask.count("}", 0, offset)
            class_depth = self.mask.count("{", 0, body + 1) - self.mask.count("}", 0, body + 1)
            if depth != class_depth:
                continue
            start = max(self.mask.rfind(token, 0, offset) for token in (";", "{", "}")) + 1
            modifiers = self.mask[start:offset]
            fields.append(
                JavaField(
                    match.group(2),
                    line,
                    owner,
                    bool(re.search(r"\bstatic\b", modifiers) and not re.search(r"\bfinal\b", modifiers)),
                    type_name=match.group(1),
                    access=next(
                        (word for word in ("private", "protected", "public") if re.search(rf"\b{word}\b", modifiers)),
                        "package",
                    ),
                )
            )
        return tuple(fields)

    def _plain_class_header(self, owner) -> str | None:
        if owner is None:
            return None
        offset = sum(len(row) for row in self.mask.splitlines(keepends=True)[: owner.line - 1])
        declaration = re.search(rf"\bclass\s+{re.escape(owner.name)}\b", self.mask[offset:])
        if declaration is None or self.mask.count("\n", 0, offset + declaration.start()) + 1 != owner.line:
            return None
        start = offset + declaration.start()
        end = self.mask.find("{", start)
        if end < 0:
            return None
        header = self.mask[start:end]
        # Generic substitution, interfaces and runtime dispatch need a type
        # system. This pointer only follows plain, explicit class declarations.
        if "<" in header or re.search(r"\bimplements\b", header):
            return None
        return header

    def supports_member_ancestry(self, owner) -> bool:
        return self._plain_class_header(owner) is not None

    def superclass(self, owner) -> str | None:
        """One explicit plain class ancestor for declaration navigation."""
        header = self._plain_class_header(owner)
        if header is None:
            return None
        parent = re.search(rf"\bextends\s+({_NAME}(?:\.{_NAME})*)\b", header)
        return self._type_name(parent.group(1)) if parent else None

    def inherited_receiver_name(self, receiver: str, line: int) -> str | None:
        """A member name with no closer local/current-class binding."""
        name = receiver.removeprefix("this.")
        if name in {"this", "super"} or not re.fullmatch(_NAME, name):
            return None
        bound, _ = self._receiver_type(receiver, line)
        return None if bound else name

    def field_type(self, field: JavaField) -> str | None:
        return self._type_name(field.type_name) if field.type_name else None

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

    def state_references(self, definition):
        """Index mutable static fields and same-class reference locations.

        Inspect the callee and one layer of local helpers. Do not infer writers,
        execution order, or completeness from this lexical index.
        """
        owner = self.owner(definition.line)
        if owner is None or definition.symbol_type != "function":
            return []
        lines = self.mask.splitlines()
        fields = [field for field in self.field_declarations if field.owner == owner and field.mutable_static]
        if not fields:
            return []
        methods = [d for d in self.functions if self.owner(d.line) == owner]
        related = [definition]
        for call in self.calls:
            if not definition.line <= call.line <= definition.end_line:
                continue
            target = self.call_target(call)
            related.extend(d for d in methods if self.qualified_definition(d) == target)
        used = set(re.findall(_NAME, "\n".join("\n".join(lines[d.line - 1 : d.end_line]) for d in related)))
        entries = []
        for field in fields:
            if field.name not in used:
                continue
            matcher = re.compile(rf"\b{re.escape(field.name)}\b")
            references = []
            for method in methods:
                for number in range(method.line, method.end_line + 1):
                    if self.owner(number) == owner and matcher.search(lines[number - 1]):
                        references.append((method, number))
                        break
            entries.append((field, references))
        return entries

    def state_navigation(self, definition, *, max_chars: int = 600) -> str:
        indexed = self.state_references(definition)
        entries = []
        for field, references in indexed[:4]:
            entries.append(
                f"{field.name}@{field.line}: "
                + ", ".join(
                    f"{self.qualified_definition(method)}@{method.line} (reference {number})"
                    for method, number in references[:6]
                )
                + ("; more omitted" if len(references) > 6 else "")
            )
        if not entries:
            return ""
        text = "State navigation (not evidence; inspect config/reset callers; order unproved): " + "; ".join(
            entries[:4]
        )
        if len(indexed) > 4:
            text += "; more fields omitted"
        marker = " ... omitted"
        return text if len(text) <= max_chars else (text[: max(0, max_chars - len(marker))] + marker)[:max_chars]
