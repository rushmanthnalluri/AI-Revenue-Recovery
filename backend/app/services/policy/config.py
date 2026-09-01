"""Policy configuration: strict loading, validation, and content versioning.

The YAML policy file is the single source of truth for the deterministic gate
(ADR 0003). Loading is fail-closed: any syntax error, missing section, unknown
key, or invalid value raises :class:`PolicyConfigError` so the process refuses
to start (or the caller installs a :func:`failsafe_config` engine that blocks
everything) instead of guessing safety limits.

`policy_version` is derived from a SHA-256 hash of the exact file bytes, so
every persisted decision is traceable to the precise configuration that
produced it.
"""

from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import settings
from app.ports import ActionType

# backend/app/services/policy/config.py -> repo root (for relative POLICY_FILE).
_REPO_ROOT = Path(__file__).resolve().parents[4]

# never_auto_execute entries that are not ActionType values but context flags:
#   irreversible_action -> ActionContext.metadata["irreversible_action"] truthy
#   customer_opted_out  -> ActionContext.customer_opted_out is True
SPECIAL_NEVER_FLAGS = frozenset({"irreversible_action", "customer_opted_out"})

_ACTION_VALUES = frozenset(a.value for a in ActionType)


class PolicyConfigError(Exception):
    """The policy file could not be read, parsed, or validated. Fail closed."""


def _check_action_names(names: list[str], *, field: str, allow_special: bool = False) -> list[str]:
    valid = []
    for raw in names:
        if not isinstance(raw, str):
            raise ValueError(f"{field}: entries must be strings, got {type(raw).__name__}")
        name = raw.strip().lower()
        if name in _ACTION_VALUES or (allow_special and name in SPECIAL_NEVER_FLAGS):
            valid.append(name)
        else:
            raise ValueError(f"{field}: unknown action {raw!r}")
    return valid


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KillSwitchConfig(_Strict):
    enabled: bool = False
    reason: str = ""
    exempt_actions: list[str] = Field(
        default_factory=lambda: [ActionType.ESCALATE_HUMAN.value, ActionType.NO_ACTION.value]
    )

    @field_validator("exempt_actions")
    @classmethod
    def _valid_exempt(cls, v: list[str]) -> list[str]:
        return _check_action_names(v, field="kill_switch.exempt_actions")


class ActionsConfig(_Strict):
    allowlist: list[str]

    @field_validator("allowlist")
    @classmethod
    def _valid_allowlist(cls, v: list[str]) -> list[str]:
        names = _check_action_names(v, field="actions.allowlist")
        if not names:
            raise ValueError("actions.allowlist must not be empty")
        return list(dict.fromkeys(names))  # dedupe, keep order


class AutoExecuteConfig(_Strict):
    min_confidence: float = Field(0.85, ge=0.0, le=1.0)
    max_amount_inr: int = Field(5000, ge=0)
    max_attempts: int = Field(2, ge=0)


class RequireHumanApprovalConfig(_Strict):
    amount_above_inr: int = Field(5000, ge=0)
    confidence_below: float = Field(0.85, ge=0.0, le=1.0)


class DuplicateProtectionConfig(_Strict):
    cooldown_minutes: int = Field(60, ge=0)


class RateLimitsConfig(_Strict):
    max_actions_per_incident: int = Field(10, ge=0)
    max_actions_per_customer_per_day: int = Field(3, ge=0)
    max_actions_global_per_hour: int = Field(100, ge=0)


class StoppingRuleConfig(_Strict):
    max_consecutive_failed_recoveries_per_incident: int = Field(3, ge=1)
    max_consecutive_failed_recoveries_per_strategy: int = Field(3, ge=1)


class ApprovalConfig(_Strict):
    # Optional approval TTL (docs/policy.md §3): a PENDING_APPROVAL action
    # whose wait EXCEEDS this many hours lapses back to PROPOSED on read
    # (executor lapse-on-read, actor system:approval_ttl). Absent = disabled:
    # approvals then wait for an explicit approve/reject, indefinitely.
    pending_approval_ttl_hours: int | None = Field(default=None, ge=1)


class PolicyConfig(_Strict):
    """Validated, immutable-by-convention policy configuration."""

    version: str
    actions: ActionsConfig
    never_auto_execute: list[str]
    auto_execute: AutoExecuteConfig
    require_human_approval: RequireHumanApprovalConfig
    stopping_rule: StoppingRuleConfig
    rate_limits: RateLimitsConfig
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    duplicate_protection: DuplicateProtectionConfig = Field(
        default_factory=DuplicateProtectionConfig
    )
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    # Set by the loader from the file hash; "unknown" for programmatic configs.
    policy_version: str = "unknown"

    @field_validator("never_auto_execute")
    @classmethod
    def _valid_never(cls, v: list[str]) -> list[str]:
        return _check_action_names(v, field="never_auto_execute", allow_special=True)

    # -- derived, paise-denominated thresholds --------------------------------
    @property
    def max_amount_paise(self) -> int:
        return self.auto_execute.max_amount_inr * 100

    @property
    def amount_above_paise(self) -> int:
        return self.require_human_approval.amount_above_inr * 100

    @property
    def auto_amount_ceiling_paise(self) -> int:
        """Stricter of the two amount bounds — misconfiguration only tightens."""
        return min(self.max_amount_paise, self.amount_above_paise)

    @property
    def auto_confidence_floor(self) -> float:
        """Stricter of the two confidence bounds."""
        return max(self.auto_execute.min_confidence, self.require_human_approval.confidence_below)

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(self.actions.allowlist)


def _resolve_path(path: str | Path | None) -> Path:
    p = Path(path) if path is not None else Path(settings.POLICY_FILE)
    if p.is_absolute():
        return p
    # Locally the policy file lives at the repo root (parents[4]); in the
    # deploy image the backend tree is flattened to /srv with policies/
    # copied alongside app/ (parents[3]). Pick the nearest ancestor that
    # actually contains the file, so both layouts resolve.
    for base in (Path(__file__).resolve().parents[3], _REPO_ROOT):
        candidate = base / p
        if candidate.exists():
            return candidate
    return _REPO_ROOT / p


def load_policy_config(path: str | Path | None = None) -> PolicyConfig:
    """Load and strictly validate the policy file. Raises PolicyConfigError.

    `path` defaults to `settings.POLICY_FILE`; relative paths resolve against
    the repo root regardless of the current working directory.
    """
    p = _resolve_path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise PolicyConfigError(f"policy file unreadable: {p}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"policy file is not valid YAML: {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyConfigError(f"policy file must be a YAML mapping: {p}")
    try:
        config = PolicyConfig.model_validate(data)
    except ValidationError as exc:
        raise PolicyConfigError(f"policy file failed validation: {p}: {exc}") from exc
    config.policy_version = f"{config.version}+sha256.{sha256(raw).hexdigest()[:12]}"
    return config


def failsafe_config(reason: str = "policy configuration unavailable") -> PolicyConfig:
    """A config that BLOCKs everything (kill switch on, no exemptions).

    Used when the real policy file cannot be loaded but an engine instance is
    still required: evaluation stays total and every decision is BLOCKED with
    policy_version "failsafe".
    """
    config = PolicyConfig(
        version="failsafe",
        actions=ActionsConfig(allowlist=[a.value for a in ActionType]),
        never_auto_execute=[*(a.value for a in ActionType), *sorted(SPECIAL_NEVER_FLAGS)],
        auto_execute=AutoExecuteConfig(),
        require_human_approval=RequireHumanApprovalConfig(),
        stopping_rule=StoppingRuleConfig(),
        rate_limits=RateLimitsConfig(),
        kill_switch=KillSwitchConfig(enabled=True, reason=reason, exempt_actions=[]),
    )
    config.policy_version = "failsafe"
    return config


__all__ = [
    "ActionsConfig",
    "ApprovalConfig",
    "AutoExecuteConfig",
    "DuplicateProtectionConfig",
    "KillSwitchConfig",
    "PolicyConfig",
    "PolicyConfigError",
    "RateLimitsConfig",
    "RequireHumanApprovalConfig",
    "SPECIAL_NEVER_FLAGS",
    "StoppingRuleConfig",
    "failsafe_config",
    "load_policy_config",
]
