from __future__ import annotations

from harness import (
    AuthorizationDecision,
    AuthorizationPolicy,
    OpenAuthorization,
    Principal,
    RiskTier,
    RoleBasedAuthorization,
    StandardAudit,
)


def test_tiers_are_ordered_so_gates_can_compare():
    """A gate configured at a tier engages for that tier and everything above."""
    assert RiskTier.ROUTINE < RiskTier.ELEVATED < RiskTier.CONSEQUENTIAL < RiskTier.CRITICAL
    assert RiskTier.CRITICAL >= RiskTier.CONSEQUENTIAL


def test_policies_satisfy_the_protocol_structurally():
    assert isinstance(OpenAuthorization(), AuthorizationPolicy)
    assert isinstance(RoleBasedAuthorization({}), AuthorizationPolicy)


def test_a_decision_can_carry_obligations():
    """An obligation is a condition of the permission, not a separate check."""
    d = AuthorizationDecision.allow("redact:ssn", "record:access")
    assert d.allowed
    assert "redact:ssn" in d.obligations


def test_denial_carries_a_reason():
    d = AuthorizationDecision.deny("no permissible purpose")
    assert not d.allowed
    assert d.reason == "no permissible purpose"


def test_principal_can_carry_a_purpose():
    """Some contexts authorise on why, not only on who."""
    p = Principal(id="u1", purpose="account-review")
    assert p.purpose == "account-review"


def test_retention_scales_with_risk():
    a = StandardAudit()
    assert a.retention_days(RiskTier.CRITICAL) > a.retention_days(RiskTier.ROUTINE)


def test_every_tier_has_a_retention_period():
    a = StandardAudit()
    for tier in RiskTier:
        assert a.retention_days(tier) > 0
