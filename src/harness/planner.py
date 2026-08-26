"""The planner side of the split.

A planner decides *what to do next*. It does not do it. It returns an
intention — call this tool with these arguments, or finish with this output —
and the executor is what actually acts.

That separation is the load-bearing decision in this design, and it exists for
three reasons:

- **The planner cannot bypass policy.** It has no reference to the registry, so
  there is no route from "the model asked for it" to "the tool ran" that skips
  authorisation. A planner that could invoke tools directly would be a policy
  bypass one refactor away.
- **Either half can be tested alone.** A planner is a pure function from state
  to intention; an executor is testable with a scripted planner and no model.
- **A failure has an owner.** When a run goes wrong you can say whether the
  plan was wrong or the execution was, which fused together you cannot.

The harness ships the protocol and one deterministic implementation. A planner
backed by a model is the same interface, and belongs to the service.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from harness.run import StepRecord


@dataclass(frozen=True)
class CallTool:
    """Intent: invoke a tool.

    `estimated_cost_usd` lets the executor check the budget *before* spending
    it. A planner that cannot estimate passes zero and the check degrades to
    an after-the-fact one, which is worse but still bounded by the ceiling.
    """

    tool: str
    arguments: dict[str, Any]
    rationale: str = ""
    estimated_cost_usd: float = 0.0
    cost_usd: float = 0.0  # cost of producing this decision


@dataclass(frozen=True)
class Finish:
    """Intent: stop, with this output."""

    output: Any
    rationale: str = ""
    cost_usd: float = 0.0


Decision = CallTool | Finish


@dataclass(frozen=True)
class PlannerState:
    """Everything a planner is allowed to see.

    Deliberately a value object rather than the live `RunContext`. A planner
    handed the context could mutate the budget it is being constrained by, and
    the constraint would then be advisory.

    `remaining_steps` and `remaining_usd` are included on purpose: a planner
    that knows it has one step left can finish with a partial answer rather
    than starting something it cannot complete.
    """

    goal: str
    tools: Sequence[dict[str, Any]]
    history: tuple[StepRecord, ...] = ()
    remaining_steps: int = 0
    remaining_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def last_error(self) -> str | None:
        """The most recent failure, if the previous step failed.

        This is the whole of self-correction from the planner's side: it sees
        what went wrong and decides what to do about it. The executor decides
        how many times it is allowed to keep deciding.
        """
        if self.history and self.history[-1].failed:
            return self.history[-1].error
        return None


@runtime_checkable
class Planner(Protocol):
    name: str

    async def decide(self, state: PlannerState) -> Decision: ...


class ScriptedPlanner:
    """Returns a fixed sequence of decisions.

    Ships with the harness rather than living in the tests because every
    service needs it: it is how you test an executor, a policy or a tool
    without a model in the loop, and how you reproduce a specific run.
    """

    name = "scripted"

    def __init__(self, *decisions: Decision) -> None:
        if not decisions:
            raise ValueError("a scripted planner needs at least one decision")
        self._decisions = list(decisions)
        self._index = 0

    async def decide(self, state: PlannerState) -> Decision:
        if self._index >= len(self._decisions):
            # Running off the end means the script did not anticipate where
            # the run went. Finishing quietly would hide that.
            return Finish(output=None, rationale="scripted planner exhausted")
        decision = self._decisions[self._index]
        self._index += 1
        return decision
