"""`python -m evals` -- a stub that fails loudly.

The template has nothing to evaluate. A service replaces this with its own
entry point, wiring its target and its evaluators into `run_eval`.

Exiting non-zero rather than printing "not configured" and exiting 0 is the
point: a service that wires the eval job into CI before wiring up an evaluator
would otherwise get a permanently green, permanently meaningless check.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "agent-harness ships no evaluators.\n\n"
        "Define your own by implementing evals.Evaluator, then call\n"
        "evals.run_eval(dataset, target, evaluators) from your own entry point.\n"
        "See docs/adr/0011-evaluation-harness.md.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
