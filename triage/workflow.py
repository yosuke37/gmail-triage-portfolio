from collections.abc import Awaitable, Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from triage.core import Email, Suggestion, preview_record
from triage.proposals import Plan, Policy, baseline_plan, validate_plan


class State(TypedDict, total=False):
    email: Email
    suggestion: Suggestion
    preview: dict
    plan: dict


def build_workflow(classify: Callable[[Email], Awaitable[Suggestion]], *, checkpointer=None, policy=Policy()):
    async def classify_node(state: State):
        return {"suggestion": await classify(state["email"])}

    def propose_node(state: State):
        suggestion = state["suggestion"]
        plan = Plan.model_validate(suggestion.proposal) if suggestion.proposal else baseline_plan(state["email"], suggestion)
        return {"plan": plan.model_dump()}

    def policy_node(state: State):
        plan = Plan.model_validate(state["plan"])
        bundle = validate_plan(state["email"], plan, policy)
        review = plan.needs_review or bool(bundle["risk_flags"]) or not all(bundle["evidence_valid"])
        return {"suggestion": Suggestion(plan.label, plan.reason, review, bundle)}

    def preview_node(state: State):
        return {"preview": preview_record(state["email"], state["suggestion"])}

    graph = StateGraph(State)
    graph.add_node("classify", classify_node)
    graph.add_node("propose", propose_node)
    graph.add_node("policy", policy_node)
    graph.add_node("preview", preview_node)
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "propose")
    graph.add_edge("propose", "policy")
    graph.add_edge("policy", "preview")
    graph.add_edge("preview", END)
    return graph.compile(checkpointer=checkpointer)
