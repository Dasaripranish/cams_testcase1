"""
MAFIA-generated graph builder for MA01 Modification Request Classifier.

HOW TO USE:
1. Copy build_mafia_graph() into your Executor class in src/agent/executor.py
2. In _run_graph(), change:
       graph = self.build_graph(tenant_id)
   to:
       graph = self.build_mafia_graph(tenant_id)

WHY: This creates a deterministic phase-by-phase workflow instead of
open-ended ReAct. Each phase runs in sequence — no phase can be skipped.
Required for financial compliance and auditability.
"""
from __future__ import annotations
from langgraph.graph import StateGraph, END
from agent.nodes.phase_1_request_intake import RequestIntakeNode
from agent.nodes.phase_2_classification import ClassificationNode
from agent.nodes.phase_3_eligibility_check import EligibilityCheckNode
from agent.nodes.phase_4_channel_routing import ChannelRoutingNode
from agent.nodes.phase_5_document_checklist import DocumentChecklistNode


def build_mafia_graph(self, tenant_id: str = "default"):
    """
    Deterministic 5-phase workflow for MA01 Modification Request Classifier.
    Each phase runs in sequence — LLM cannot skip or reorder phases.

    Phases:
        1. Request Intake
        2. Classification
        3. Eligibility Check
        4. Channel Routing
        5. Document Checklist
    """
    from agent.nodes import AgentState
    config = self._get_tenant_config(tenant_id)
    llm = self._get_llm(tenant_id)
    tools = self.tool_registry.get_tools(tenant_id)

    graph = StateGraph(AgentState)

    # Register phase nodes
    graph.add_node("request_intake", RequestIntakeNode(llm, tools, config))
    graph.add_node("classification", ClassificationNode(llm, tools, config))
    graph.add_node("eligibility_check", EligibilityCheckNode(llm, tools, config))
    graph.add_node("channel_routing", ChannelRoutingNode(llm, tools, config))
    graph.add_node("document_checklist", DocumentChecklistNode(llm, tools, config))

    # Wire phases in sequence — deterministic, no branching
    graph.set_entry_point("request_intake")
    graph.add_edge("request_intake", "classification")
    graph.add_edge("classification", "eligibility_check")
    graph.add_edge("eligibility_check", "channel_routing")
    graph.add_edge("channel_routing", "document_checklist")
    graph.add_edge("document_checklist", END)

    return graph.compile()