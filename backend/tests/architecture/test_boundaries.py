"""Architecture conformance: static import-direction analysis over `app/`.

ADR 0010 makes the dependency matrix executable. This test parses every
`app/**/*.py` module's AST (no imports are executed, so it cannot be gamed by
lazy/`TYPE_CHECKING` imports) and fails if any module imports a package the
matrix forbids. The rules below are the single source of truth for the
sanctioned dependency directions; `docs/architecture.md` mirrors them.

Reading the table: "modules under `package` must NOT import anything under any
of `forbidden`". Edges to `app.models`, `app.ports`, `app.config`, `app.db`,
`app.ids`, `app.logging`, and `app.schemas` are unrestricted shared contracts.

Sanctioned edges (deliberately NOT forbidden — do not "fix" the services):
- agent -> services.{diagnosis, policy, revenue}: the investigator composes the
  diagnosis service, writes audit rows via policy.audit, and its tools gate
  through the policy engine / read revenue. Only razorpay/simulator/api are
  off-limits (the agent must never touch money movement or ingress).
- recovery -> services.{policy, revenue, razorpay.errors}: the executor IS the
  policy-gate caller and strategies price via the revenue engine; the gateway
  error types are a leaf module (no service imports) shared via the port.
- detection -> app.schemas: the engine accepts the DetectionRunRequest
  contract object (schemas are the shared frontend contract, not a service).
- diagnosis -> services.detection: the window re-scoping triage (rescope.py)
  rebuilds the incident's own metric series with the detection series helpers
  (same loaders, same bucket grid, same floors) instead of re-implementing
  them; the edge stays one-directional (detection never imports back).
- merchant -> services.razorpay: the real-sync service composes the gateway
  adapter's typed errors to pull the merchant's real Razorpay data; agent,
  api, simulator, and every other service package are off-limits (it must
  never touch the recovery loop or the research sandbox).
- evaluation (harness, see HARNESS_PACKAGES below) -> services, simulator, and
  app.api.v1.webhooks.EVENT_HANDLERS: the harness is a second composition root
  for experiments; it deliberately reuses the real verification path. Not on
  the serving path, so it carries no import-direction rule.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# Packages every service leaf may NOT import: any other service package, the
# API layer, and the simulator. `self` is removed per-package below.
_ALL_SERVICE_PACKAGES = (
    "app.services.agent",
    "app.services.audit",
    "app.services.detection",
    "app.services.diagnosis",
    "app.services.evaluation",
    "app.services.insights",
    "app.services.merchant",
    "app.services.policy",
    "app.services.razorpay",
    "app.services.recovery",
    "app.services.revenue",
    "app.services.worker",
)


def _leaf_forbidden(package: str) -> tuple[str, ...]:
    return tuple(p for p in _ALL_SERVICE_PACKAGES if p != package) + (
        "app.api",
        "app.simulator",
    )


@dataclass(frozen=True)
class Rule:
    package: str
    forbidden: tuple[str, ...]
    why: str


RULES: tuple[Rule, ...] = (
    Rule(
        "app.services.agent",
        ("app.services.razorpay", "app.simulator", "app.api"),
        "advisory only: never touches the gateway adapter, the simulator, or ingress",
    ),
    Rule(
        "app.services.policy",
        _leaf_forbidden("app.services.policy"),
        "the deterministic core: models/ports/config/db/ids/logging only",
    ),
    Rule(
        "app.services.recovery",
        ("app.services.agent", "app.api", "app.simulator"),
        "execution engine: policy-gated, but never calls the agent, API, or simulator",
    ),
    Rule(
        "app.services.razorpay",
        _leaf_forbidden("app.services.razorpay"),
        "leaf adapter: translates the PaymentGateway port, knows nothing upstream",
    ),
    Rule(
        "app.services.revenue",
        _leaf_forbidden("app.services.revenue"),
        "leaf: pure quantification over the shared models",
    ),
    Rule(
        "app.services.detection",
        _leaf_forbidden("app.services.detection"),
        "leaf: payment_events in, incidents out",
    ),
    Rule(
        "app.services.diagnosis",
        tuple(
            p
            for p in _ALL_SERVICE_PACKAGES
            if p not in ("app.services.diagnosis", "app.services.detection")
        )
        + ("app.api", "app.simulator"),
        "triage composer: features + model artifacts in, diagnoses out; "
        "re-scopes diluted detection windows via the detection series "
        "helpers (rescope.py) — never the agent, api, simulator, or other "
        "services",
    ),
    Rule(
        "app.services.insights",
        _leaf_forbidden("app.services.insights"),
        "leaf: payment_events in, ranked failure-facet outliers out",
    ),
    Rule(
        "app.services.merchant",
        tuple(
            p
            for p in _ALL_SERVICE_PACKAGES
            if p not in ("app.services.merchant", "app.services.razorpay")
        )
        + ("app.api", "app.simulator"),
        "composing service: real Razorpay sync — may use the razorpay adapter's "
        "typed errors; never the agent, api, simulator, or other services",
    ),
    Rule(
        "app.services.worker",
        tuple(
            p
            for p in _ALL_SERVICE_PACKAGES
            if p not in ("app.services.worker", "app.services.recovery", "app.services.policy")
        )
        + ("app.api", "app.simulator"),
        "composing service: the scheduler tier (docs/worker.md) drives the "
        "recovery executor/sweep and audits via policy.audit; never the agent, "
        "api, simulator, or other services",
    ),
    Rule(
        "app.services.audit",
        _leaf_forbidden("app.services.audit"),
        "leaf: read-only integrity verification over the shared audit models",
    ),
    Rule(
        "app.simulator",
        ("app.services",),
        "the synthetic environment must not depend on the system it feeds",
    ),
    # app.api.* is the composition root: it MAY import services, so no rule.
)

# Packages exempt from the matrix, with the reason the exemption is sanctioned.
HARNESS_PACKAGES = {
    "app.services.evaluation": (
        "experiment harness — a second composition root that deliberately "
        "drives the simulator, the services, and the webhook registry; "
        "never on the serving path"
    ),
}


@dataclass
class ImportEdge:
    src_module: str
    src_file: Path
    lineno: int
    dst_module: str

    def render(self) -> str:
        rel = self.src_file.relative_to(APP_ROOT.parent)
        return f"{rel}:{self.lineno}  {self.src_module}  ->  {self.dst_module}"


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(src_module: str, is_package: bool, level: int, module: str | None) -> str:
    """Resolve `from . import x` / `from ..y import z` to an absolute module."""
    parts = src_module.split(".")
    # For a regular module the package is one level up; `level` beyond that
    # walks further up. __init__.py's module IS the package.
    base = parts if is_package else parts[:-1]
    if level > 1:
        base = base[: -(level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _collect_edges(path: Path) -> list[ImportEdge]:
    src_module = _module_name(path)
    is_package = path.name == "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(src_module, path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(src_module, is_package, node.level, node.module)
            else:
                base = node.module or ""
            edges.append(ImportEdge(src_module, path, node.lineno, base))
            # `from app import ids` depends on app.ids, not just app — record
            # the submodule edge too so nothing hides behind a package import.
            for alias in node.names:
                if alias.name != "*":
                    edges.append(
                        ImportEdge(src_module, path, node.lineno, f"{base}.{alias.name}")
                    )
    return edges


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _violations(rule: Rule, edges: list[ImportEdge]) -> list[ImportEdge]:
    return [
        edge
        for edge in edges
        if _under(edge.src_module, rule.package)
        and any(_under(edge.dst_module, f) for f in rule.forbidden)
    ]


@pytest.fixture(scope="module")
def all_edges() -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        edges.extend(_collect_edges(path))
    return edges


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.package)
def test_import_direction(rule: Rule, all_edges: list[ImportEdge]) -> None:
    violations = _violations(rule, all_edges)
    if violations:
        report = "\n".join(f"  {v.render()}" for v in violations)
        pytest.fail(
            f"{rule.package} imports a forbidden package ({rule.why}).\n"
            f"Forbidden: {', '.join(rule.forbidden)}\nViolations:\n{report}\n"
            "If this edge is intentional, get it sanctioned in ADR 0010 and "
            "encode the sanction here — do not bypass the matrix."
        )


def test_every_service_package_is_classified() -> None:
    """A new app.services.* package must be ruled or explicitly exempted —
    no package may slip into the tree unclassified."""
    services_dir = APP_ROOT / "services"
    on_disk = {
        f"app.services.{p.name}"
        for p in services_dir.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }
    classified = {r.package for r in RULES} | set(HARNESS_PACKAGES)
    unclassified = on_disk - classified
    assert not unclassified, (
        f"service packages with no dependency-direction rule: {sorted(unclassified)}. "
        "Add a Rule (or a sanctioned HARNESS_PACKAGES exemption) to "
        "tests/architecture/test_boundaries.py."
    )


def test_matrix_documents_itself() -> None:
    """Guard against rule-table drift: every rule targets app.* and no rule
    forbids the package it governs (self-imports are always allowed)."""
    for rule in RULES:
        assert rule.package.startswith("app."), rule
        assert rule.forbidden, f"{rule.package}: empty forbidden set is a no-op rule"
        assert rule.package not in rule.forbidden, f"{rule.package} forbids itself"
