"""Tool registry tests.

The registry is the choke point. If a call can reach a tool without passing
through it, the audit trail has a hole — so most of these tests are about
what the registry refuses.
"""

from __future__ import annotations

import pytest

from harness import (
    AuthorizationDecision,
    Principal,
    RateLimitExceededError,
    RiskTier,
    RoleBasedAuthorization,
    ToolDeniedError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
)

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}


def _spec(name: str, **kw) -> ToolSpec:
    return ToolSpec(name=name, description=f"the {name} tool", parameters=OBJ, **kw)


async def _echo(arguments: dict) -> dict:
    return {"echoed": arguments}


ANYONE = Principal(id="u1", roles=frozenset({"analyst"}))


# ------------------------------------------------------------------ ToolSpec
def test_a_tool_needs_a_name():
    with pytest.raises(ValueError, match="needs a name"):
        ToolSpec(name="", description="x", parameters=OBJ)


def test_parameters_must_be_an_object_schema():
    """Tool-calling APIs expect an object at the top level."""
    with pytest.raises(ValueError, match="JSON Schema object"):
        ToolSpec(name="t", description="x", parameters={"type": "string"})


# ------------------------------------------------------------------ register
def test_registering_twice_is_an_error():
    """Silent replacement lets a later import shadow a tool policy was written against."""
    r = ToolRegistry()
    r.register(_spec("search"), _echo)
    with pytest.raises(ValueError, match="already registered"):
        r.register(_spec("search"), _echo)


def test_registry_reports_its_contents():
    r = ToolRegistry()
    r.register(_spec("b"), _echo)
    r.register(_spec("a"), _echo)
    assert r.names == ("a", "b")
    assert len(r) == 2
    assert "a" in r
    assert "zzz" not in r


def test_unknown_tool_raises():
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().spec("nope")


# --------------------------------------------------------------- authorization
async def test_open_policy_permits_by_default():
    r = ToolRegistry()
    r.register(_spec("search"), _echo)
    assert await r.invoke("search", {"q": "x"}, ANYONE) == {"echoed": {"q": "x"}}


async def test_role_policy_denies_a_principal_without_the_role():
    r = ToolRegistry(RoleBasedAuthorization({"payments": frozenset({"treasury"})}))
    r.register(_spec("payments"), _echo)
    with pytest.raises(ToolDeniedError) as exc:
        await r.invoke("payments", {}, ANYONE)
    assert exc.value.tool_name == "payments"


async def test_an_ungranted_tool_is_denied_not_permitted():
    """An unlisted tool is far more often an oversight than an open one."""
    r = ToolRegistry(RoleBasedAuthorization({}))
    r.register(_spec("search"), _echo)
    with pytest.raises(ToolDeniedError, match="not granted"):
        await r.invoke("search", {}, ANYONE)


async def test_policy_sees_the_arguments_not_just_the_tool_name():
    """Authorisation often depends on what is being asked for, not only what is called."""
    seen: list[dict] = []

    class Recording:
        name = "recording"

        def authorize(self, principal, tool_name, arguments):
            seen.append(arguments)
            return AuthorizationDecision.allow()

    r = ToolRegistry(Recording())
    r.register(_spec("search"), _echo)
    await r.invoke("search", {"q": "consumer-123"}, ANYONE)
    assert seen[-1] == {"q": "consumer-123"}


def test_describe_for_hides_tools_the_principal_cannot_call():
    """A planner shown a tool it cannot call will plan to call it, then fail."""
    r = ToolRegistry(
        RoleBasedAuthorization(
            {
                "search": frozenset({"analyst"}),
                "payments": frozenset({"treasury"}),
            }
        )
    )
    r.register(_spec("search"), _echo)
    r.register(_spec("payments"), _echo)
    visible = [t["name"] for t in r.describe_for(ANYONE)]
    assert visible == ["search"]


def test_describe_returns_the_schema_the_registry_validates_against():
    r = ToolRegistry()
    r.register(_spec("search"), _echo)
    assert r.describe_for(ANYONE)[0]["parameters"] == OBJ


# ---------------------------------------------------------------- rate limits
async def test_rate_limit_is_enforced():
    r = ToolRegistry()
    r.register(_spec("search", rate_limit_per_minute=2), _echo)
    await r.invoke("search", {}, ANYONE)
    await r.invoke("search", {}, ANYONE)
    with pytest.raises(RateLimitExceededError):
        await r.invoke("search", {}, ANYONE)


async def test_no_rate_limit_means_no_limit():
    r = ToolRegistry()
    r.register(_spec("search"), _echo)
    for _ in range(50):
        await r.invoke("search", {}, ANYONE)


def test_tier_defaults_to_routine_and_is_declarable():
    assert _spec("a").tier is RiskTier.ROUTINE
    assert _spec("b", tier=RiskTier.CRITICAL).tier is RiskTier.CRITICAL
