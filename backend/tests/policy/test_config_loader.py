"""Policy file loading: strict validation, INR->paise conversion, and the
content-hash policy version. Broken configs must fail CLOSED (raise), never
silently default to something permissive.
"""

import shutil

import pytest
import yaml

from app.services.policy import PolicyConfigError, load_policy_config
from app.services.policy.config import failsafe_config

_REAL_POLICY = "policies/default.yaml"


def _write_variant(tmp_path, mutate) -> str:
    with open(_real_path(), "rb") as fh:
        data = yaml.safe_load(fh.read())
    mutate(data)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _real_path() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[3] / "policies" / "default.yaml")


class TestRealDefaultFile:
    def test_loads_and_converts_thresholds(self):
        config = load_policy_config()
        assert config.version == "1.0"
        assert config.auto_execute.min_confidence == 0.85
        assert config.max_amount_paise == 500_000  # INR 5000 -> paise
        assert config.amount_above_paise == 500_000
        assert config.auto_amount_ceiling_paise == 500_000
        assert config.auto_confidence_floor == 0.85
        assert config.auto_execute.max_attempts == 2

    def test_allowlist_is_closed_and_excludes_refund(self):
        config = load_policy_config()
        assert "retry_payment" in config.allowlist
        assert "create_payment_link" in config.allowlist
        assert "no_action" in config.allowlist
        assert "refund" not in config.allowlist

    def test_hard_blocks_and_stopping_rule(self):
        config = load_policy_config()
        assert set(config.never_auto_execute) == {
            "refund",
            "irreversible_action",
            "customer_opted_out",
        }
        assert config.stopping_rule.max_consecutive_failed_recoveries_per_incident == 3
        assert config.stopping_rule.max_consecutive_failed_recoveries_per_strategy == 3
        assert config.rate_limits.max_actions_per_customer_per_day == 3
        assert config.duplicate_protection.cooldown_minutes == 60
        assert config.kill_switch.enabled is False

    def test_policy_version_is_content_addressed(self):
        v1 = load_policy_config().policy_version
        v2 = load_policy_config().policy_version
        assert v1 == v2  # deterministic for identical bytes
        assert v1.startswith("1.0+sha256.")

    def test_version_changes_with_content(self, tmp_path):
        variant = tmp_path / "variant.yaml"
        shutil.copy(_real_path(), variant)
        with variant.open("a", encoding="utf-8") as fh:
            fh.write("\n# a comment still changes the hash\n")
        assert load_policy_config(variant).policy_version != load_policy_config().policy_version

    def test_relative_path_resolves_from_repo_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # cwd must not matter
        assert load_policy_config(_REAL_POLICY).version == "1.0"

    def test_approval_ttl_is_optional_and_absent_by_default(self):
        # policies/default.yaml ships without an approval section: the lapse
        # is disabled and the loader still validates (strict unknown-key
        # rules mean the section must be explicitly modeled to exist).
        assert load_policy_config().approval.pending_approval_ttl_hours is None

    def test_approval_ttl_loads_when_present(self, tmp_path):
        path = _write_variant(
            tmp_path,
            lambda d: d.__setitem__("approval", {"pending_approval_ttl_hours": 24}),
        )
        config = load_policy_config(path)
        assert config.approval.pending_approval_ttl_hours == 24

    def test_approval_ttl_rejects_unknown_keys_and_bad_values(self, tmp_path):
        path = _write_variant(
            tmp_path,
            lambda d: d.__setitem__("approval", {"pending_approval_ttl_hours": 0}),
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)
        path = _write_variant(
            tmp_path,
            lambda d: d.__setitem__("approval", {"pending_approval_ttl_hours": 1, "bogus": 2}),
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)


class TestFailClosed:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(PolicyConfigError):
            load_policy_config(tmp_path / "nope.yaml")

    def test_broken_yaml_raises(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("auto_execute: [unclosed\n  nonsense: {", encoding="utf-8")
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_non_mapping_yaml_raises(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_missing_safety_section_raises(self, tmp_path):
        path = _write_variant(tmp_path, lambda d: d.pop("never_auto_execute"))
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_unknown_top_level_key_raises(self, tmp_path):
        path = _write_variant(tmp_path, lambda d: d.__setitem__("bogus_key", 1))
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_unknown_nested_key_raises(self, tmp_path):
        # a typo must not silently drop a safety threshold onto a default
        path = _write_variant(
            tmp_path, lambda d: d["auto_execute"].__setitem__("min_confidance", 0.9)
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_unknown_allowlist_entry_raises(self, tmp_path):
        path = _write_variant(
            tmp_path, lambda d: d["actions"]["allowlist"].append("launch_nukes")
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_unknown_never_auto_execute_entry_raises(self, tmp_path):
        path = _write_variant(
            tmp_path, lambda d: d["never_auto_execute"].append("definitely_not_a_flag")
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_empty_allowlist_raises(self, tmp_path):
        path = _write_variant(tmp_path, lambda d: d["actions"].__setitem__("allowlist", []))
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)

    def test_out_of_range_confidence_raises(self, tmp_path):
        path = _write_variant(
            tmp_path, lambda d: d["auto_execute"].__setitem__("min_confidence", 1.7)
        )
        with pytest.raises(PolicyConfigError):
            load_policy_config(path)


class TestFailsafeConfig:
    def test_failsafe_blocks_everything(self):
        config = failsafe_config("unit test")
        assert config.kill_switch.enabled is True
        assert config.kill_switch.exempt_actions == []
        assert config.policy_version == "failsafe"
