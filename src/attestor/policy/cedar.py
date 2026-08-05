"""A Cedar subset: parser and evaluator, offline.

Authorization is decided **before** a tool runs, by a policy a human can read, in a language
that is not Python. That last part is deliberate. If the rules lived in the same code that
enforces them, "is this policy correct?" and "is this code correct?" would be the same
question, and reviewing either would tell you nothing about the other.

So policies are real `.cedar` files under `policy/cedar/`, and this module parses and
evaluates the subset they use. Two independent artefacts, cross-checked against a shared
decision table in `tests/policy/` — the same shape as an analyzer checked against an OPA
policy, which is the pattern that has caught real bugs before.

The subset, stated precisely so nobody assumes more than is here:

- ``permit`` and ``forbid`` policies, each optionally annotated ``@id("...")``
- scope clauses ``principal``, ``principal == E``, ``principal in E``; likewise ``resource``;
  and ``action``, ``action == A``, ``action in [A, ...]``
- ``when { ... }`` and ``unless { ... }`` conditions, conjunctions of ``&&``
- comparisons ``a.b == c.d``, ``a.b == "literal"``, ``a.b != ...``, ``a.b in ["x", "y"]``
- entity hierarchy through a parent map, so ``principal in Group::"x"`` works

Anything outside that is a parse error rather than a silent no-op. A policy that fails to
parse is a policy that would otherwise have failed open, and failing open is how an
authorization layer becomes decoration.

Evaluation follows Cedar: **forbid always wins, and the default is deny.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

CEDAR_DIR = "policy/cedar"


class Effect(StrEnum):
    PERMIT = "permit"
    FORBID = "forbid"


class PolicyError(ValueError):
    """The policy text is outside the supported subset. Never ignored, never skipped."""


@dataclass(frozen=True, slots=True)
class Entity:
    type: str
    id: str

    def __str__(self) -> str:
        return f'{self.type}::"{self.id}"'


@dataclass(frozen=True, slots=True)
class Principal:
    entity: Entity
    attributes: dict[str, Any] = field(default_factory=dict)
    parents: frozenset[Entity] = frozenset()


@dataclass(frozen=True, slots=True)
class Resource:
    entity: Entity
    attributes: dict[str, Any] = field(default_factory=dict)
    parents: frozenset[Entity] = frozenset()


@dataclass(frozen=True, slots=True)
class Request:
    principal: Principal
    action: str
    resource: Resource
    context: dict[str, Any] = field(default_factory=dict)


# ── Conditions ───────────────────────────────────────────────────────────────

_COMPARISON = re.compile(
    r"^(?P<left>[a-z]+(?:\.[a-zA-Z_][\w]*)+)\s*"
    r"(?P<op>==|!=|\bin\b)\s*"
    r"(?P<right>.+)$"
)


@dataclass(frozen=True, slots=True)
class Comparison:
    left: str
    op: str
    right: str

    def evaluate(self, request: Request) -> bool:
        left = _lookup(self.left, request)
        match self.op:
            case "==":
                return left == _literal(self.right, request)
            case "!=":
                return left != _literal(self.right, request)
            case "in":
                return left in _set_literal(self.right)
        raise PolicyError(f"unsupported operator {self.op!r}")  # pragma: no cover


def _lookup(path: str, request: Request) -> Any:
    head, _, attribute = path.partition(".")
    match head:
        case "principal":
            source = request.principal.attributes
        case "resource":
            source = request.resource.attributes
        case "context":
            source = request.context
        case _:
            raise PolicyError(f"unknown reference {head!r}")
    # A missing attribute is not `None`. Cedar treats it as an error, and so do we: a
    # condition that silently compares against a missing attribute is how a typo becomes an
    # allow.
    if attribute not in source:
        raise PolicyError(f"{path} is not present on the request")
    return source[attribute]


def _literal(text: str, request: Request) -> Any:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text in {"true", "false"}:
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return _lookup(text, request)


def _set_literal(text: str) -> set[Any]:
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise PolicyError(f"expected a set literal, got {text!r}")
    inner = text[1:-1].strip()
    if not inner:
        return set()
    return {part.strip().strip('"') for part in inner.split(",")}


# ── Policies ─────────────────────────────────────────────────────────────────

_ENTITY = re.compile(r'^(?P<type>[A-Z]\w*)::"(?P<id>[^"]+)"$')
#: DOTALL because a set of actions is normally written one per line.
_SCOPE = re.compile(
    r"^(?P<var>principal|action|resource)(?:\s+(?P<op>in|==)\s+(?P<value>.+))?$", re.DOTALL
)


@dataclass(frozen=True, slots=True)
class Scope:
    variable: str
    op: str | None = None
    entities: tuple[Entity, ...] = ()

    def matches(self, request: Request) -> bool:
        if self.op is None:
            return True
        actual, parents = _subject(self.variable, request)
        if self.op == "==":
            return actual == self.entities[0]
        return actual in self.entities or bool(parents & set(self.entities))


def _subject(variable: str, request: Request) -> tuple[Entity, frozenset[Entity]]:
    match variable:
        case "principal":
            return request.principal.entity, request.principal.parents
        case "resource":
            return request.resource.entity, request.resource.parents
        case "action":
            return Entity("Action", request.action), frozenset()
    raise PolicyError(f"unknown scope variable {variable!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    effect: Effect
    scopes: tuple[Scope, ...]
    when: tuple[Comparison, ...] = ()
    unless: tuple[Comparison, ...] = ()
    source: str = ""

    def applies(self, request: Request) -> bool:
        if not all(scope.matches(request) for scope in self.scopes):
            return False
        if not all(condition.evaluate(request) for condition in self.when):
            return False
        return not any(condition.evaluate(request) for condition in self.unless)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    #: Which policies fired. An allow with no reason is an allow nobody can explain.
    determining: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class PolicySet:
    def __init__(self, policies: tuple[Policy, ...]) -> None:
        self._policies = policies

    def __iter__(self):
        return iter(self._policies)

    def __len__(self) -> int:
        return len(self._policies)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(policy.id for policy in self._policies)

    def is_authorized(self, request: Request) -> Decision:
        """Forbid wins; default is deny."""
        forbids = [p.id for p in self._policies if p.effect is Effect.FORBID and p.applies(request)]
        if forbids:
            return Decision(False, tuple(forbids), "explicitly forbidden")
        permits = [p.id for p in self._policies if p.effect is Effect.PERMIT and p.applies(request)]
        if permits:
            return Decision(True, tuple(permits), "permitted")
        return Decision(False, (), "no policy permits this request")


# ── Parsing ──────────────────────────────────────────────────────────────────

_POLICY = re.compile(
    r"(?:@id\(\"(?P<id>[^\"]+)\"\)\s*)?"
    r"(?P<effect>permit|forbid)\s*\((?P<scope>.*?)\)"
    r"(?P<conditions>(?:\s*(?:when|unless)\s*\{[^}]*\})*)\s*;",
    re.DOTALL,
)
_CONDITION = re.compile(r"(?P<keyword>when|unless)\s*\{(?P<body>[^}]*)\}", re.DOTALL)
_COMMENT = re.compile(r"//[^\n]*")


def parse(text: str, *, source: str = "<memory>") -> tuple[Policy, ...]:
    """Every policy in the file, or an error. There is no third outcome.

    The "or an error" is the whole point and it was, for a while, not true. `_POLICY` matched
    the policies it could and the rest of the file was simply not there — so a missing
    semicolon, a typo in `forbid`, or a condition using an operator outside the subset made a
    policy *vanish*, and the only symptom was one fewer entry in a list nobody counted.

    A vanished `permit` is a confusing afternoon. A vanished `forbid` is an authorization
    layer that has quietly stopped separating tenants while every probe still reports
    `closed`, because the probes evaluate the policies that loaded.

    So the residue is checked: everything outside a matched policy must be whitespace.
    """
    stripped = _COMMENT.sub("", text)
    policies: list[Policy] = []
    residue: list[str] = []
    cursor = 0
    for index, match in enumerate(_POLICY.finditer(stripped)):
        residue.append(stripped[cursor : match.start()])
        cursor = match.end()
        scopes = tuple(_parse_scope(part) for part in _split_scope(match.group("scope")))
        when: list[Comparison] = []
        unless: list[Comparison] = []
        for condition in _CONDITION.finditer(match.group("conditions") or ""):
            target = when if condition.group("keyword") == "when" else unless
            target.extend(_parse_conditions(condition.group("body")))
        policies.append(
            Policy(
                id=match.group("id") or f"{source}#{index}",
                effect=Effect(match.group("effect")),
                scopes=scopes,
                when=tuple(when),
                unless=tuple(unless),
                source=source,
            )
        )
    residue.append(stripped[cursor:])
    unparsed = "".join(residue).strip()
    if unparsed:
        excerpt = " ".join(unparsed.split())[:160]
        raise PolicyError(
            f"{source}: {len(unparsed.split())} token(s) outside any policy this parser "
            f"recognises, beginning {excerpt!r}. A policy that does not parse is a policy "
            "that is not enforced, and a forbid that is not enforced fails open — so the "
            "file is refused rather than partially loaded."
        )
    return tuple(policies)


def _split_scope(text: str) -> list[str]:
    """Split on commas that are not inside a set literal."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _parse_scope(text: str) -> Scope:
    match = _SCOPE.match(text.strip())
    if not match:
        raise PolicyError(f"unsupported scope clause {text.strip()!r}")
    if not match.group("op"):
        return Scope(match.group("var"))
    value = match.group("value").strip()
    entities = (
        tuple(_parse_entity(part) for part in _split_scope(value[1:-1]))
        if value.startswith("[")
        else (_parse_entity(value),)
    )
    return Scope(match.group("var"), match.group("op"), entities)


def _parse_entity(text: str) -> Entity:
    match = _ENTITY.match(text.strip())
    if not match:
        raise PolicyError(f'expected an entity like Type::"id", got {text.strip()!r}')
    return Entity(match.group("type"), match.group("id"))


def _parse_conditions(body: str) -> list[Comparison]:
    comparisons: list[Comparison] = []
    for raw in body.split("&&"):
        clause = raw.strip()
        if not clause:
            continue
        match = _COMPARISON.match(clause)
        if not match:
            raise PolicyError(f"unsupported condition {clause!r}")
        comparisons.append(
            Comparison(match.group("left"), match.group("op"), match.group("right").strip())
        )
    return comparisons


def load(root: Path | str = ".") -> PolicySet:
    """Load every `.cedar` file under `policy/cedar/`."""
    directory = Path(root) / CEDAR_DIR
    policies: list[Policy] = []
    for path in sorted(directory.rglob("*.cedar")):
        policies.extend(parse(path.read_text(encoding="utf-8"), source=path.name))
    if not policies:
        raise PolicyError(f"no policies found under {directory}")
    return PolicySet(tuple(policies))
