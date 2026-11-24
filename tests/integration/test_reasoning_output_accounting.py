"""A bad reasoning answer never erases an original known provider bill."""

from uuid import uuid4

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.reasoning_investigation import ReasoningInvestigator
from potbot.controller.reasoning_runtime import PostgresReasoningStore
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.errors import InvalidInputError
from tests.integration.test_reasoning_budget_postgres import _journal, _task

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "text",
    [
        '{"hypotheses":[{"hypothesis":"A possibility","assumptions":[],"questions":[{"channel":[],"query":"q"}]}]}',
        '{"hypotheses":[],"execute":"shell"}',
    ],
)
def test_invalid_role_output_keeps_known_charge_and_failed_run_without_retry(
    app_env, tmp_path, text
):
    task = _task(app_env)
    journal = _journal(app_env)
    run = uuid4().hex
    calls = []
    investigator = ReasoningInvestigator(
        root=tmp_path / "reasoning",
        state_store=PostgresReasoningStore(app_env.db),
        budget_journal=journal,
    )

    def invoke(*_args):
        calls.append("synthetic-provider-completion")
        return text, {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, "stop"

    with pytest.raises(InvalidInputError):
        investigator.run(
            context=app_env.context,
            run_id=run,
            task="Investigate safely",
            system="Retain known evidence",
            models=[{"provider": "openai", "model": "reasoner-test", "protocol": "responses"}],
            options={"max_output_tokens": 128, "potbot_task_id": str(task)},
            invoke=invoke,
            on_text=lambda _: None,
            emit=lambda *_a, **_k: None,
        )
    amounts = BudgetLedger(app_env.db).inspect(app_env.context, task)
    assert (
        amounts["used"]["calls"] == 1 and amounts["used"]["tokens"] == 14 and amounts["held"] == {}
    )
    assert [r["status"] for r in journal.list_for_run(app_env.context, run)] == ["settled"]
    tree = ReasoningTree.load(
        investigator.root / str(app_env.context.workspace_id) / (run + ".json")
    )
    assert tree.run_status == "failed" and len(calls) == 1
    # Reusing the run is refused rather than resending a completed bad answer.
    with pytest.raises(InvalidInputError):
        investigator.run(
            context=app_env.context,
            run_id=run,
            task="Investigate safely",
            system="Retain known evidence",
            models=[{"provider": "openai", "model": "reasoner-test", "protocol": "responses"}],
            options={"max_output_tokens": 128, "potbot_task_id": str(task)},
            invoke=invoke,
            on_text=lambda _: None,
            emit=lambda *_a, **_k: None,
        )
    assert len(calls) == 1
