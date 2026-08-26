"""Guardrail contract tests.

The template ships no guardrails, so these exercise the interface with fakes --
which is also how a service should test its own.
"""

from __future__ import annotations

import pytest

from app.guardrails import Guardrail, GuardrailBlockedError, GuardrailChain, GuardrailResult


class _Allow:
    name = "allow"

    async def check(self, text: str) -> GuardrailResult:
        return GuardrailResult.allow()


class _Block:
    name = "block"

    async def check(self, text: str) -> GuardrailResult:
        return GuardrailResult.block("policy violation", category="test")


class _Redact:
    name = "redact"

    async def check(self, text: str) -> GuardrailResult:
        return GuardrailResult.redact(text.replace("secret", "[REDACTED]"), "found a secret")


class _Recorder:
    def __init__(self) -> None:
        self.name = "recorder"
        self.seen: list[str] = []

    async def check(self, text: str) -> GuardrailResult:
        self.seen.append(text)
        return GuardrailResult.allow()


def test_protocol_is_satisfied_by_duck_typing():
    assert isinstance(_Allow(), Guardrail)


async def test_empty_chain_passes_text_through():
    text, results = await GuardrailChain().run("hello")
    assert text == "hello"
    assert results == []


async def test_block_raises_with_the_guardrail_name():
    with pytest.raises(GuardrailBlockedError) as exc:
        await GuardrailChain(_Allow(), _Block(), _Allow()).run("hello")
    assert exc.value.guardrail_name == "block"
    assert exc.value.result.metadata["category"] == "test"


async def test_redaction_replaces_text_without_blocking():
    text, results = await GuardrailChain(_Redact()).run("my secret value")
    assert text == "my [REDACTED] value"
    assert results[0].allowed is True


async def test_later_guardrails_see_the_redacted_text():
    """Order matters: a redaction must be visible to everything downstream."""
    recorder = _Recorder()
    await GuardrailChain(_Redact(), recorder).run("my secret value")
    assert recorder.seen == ["my [REDACTED] value"]


async def test_chain_stops_at_the_first_block():
    recorder = _Recorder()
    with pytest.raises(GuardrailBlockedError):
        await GuardrailChain(_Block(), recorder).run("hello")
    assert recorder.seen == []


def test_names_are_reportable():
    assert GuardrailChain(_Allow(), _Block()).names == ["allow", "block"]
