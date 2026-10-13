"""Agents: intent classification, routing and grounded answering."""

from __future__ import annotations

from askback.core.contracts import ChatRequest, Citation, ResponseStatus, RetrievedChunk
from askback_sim.agents.base import Turn
from askback_sim.agents.frontline import FrontLineAgent, IntentSpec, KeywordIntentClassifier
from askback_sim.agents.specialized import (
    EscalationAgent,
    FallbackAgent,
    KnowledgeAgent,
    extract_citations,
)
from askback_sim.llm.extractive import ExtractiveProvider
from askback_sim.tools import build_toolbox


class TestIntentClassifier:
    def test_matches_by_keyword(self, intents: list[IntentSpec]) -> None:
        intent = KeywordIntentClassifier(intents).classify("How much annual leave do I get?")
        assert intent.name == "leave_policy"
        assert intent.confidence > 0

    def test_unknown_below_threshold(self, intents: list[IntentSpec]) -> None:
        intent = KeywordIntentClassifier(intents).classify("What is the airspeed of a swallow?")
        assert intent.name == "unknown"
        assert intent.rationale, "an unmatched turn should explain itself"

    def test_scores_are_exposed_for_debugging(self, intents: list[IntentSpec]) -> None:
        intent = KeywordIntentClassifier(intents).classify("expense claim deadline")
        assert set(intent.scores) == {spec.name for spec in intents}, (
            "every configured intent must be scored, so a misroute can be explained"
        )

    def test_hyphenated_keyword_matches_as_a_phrase(self) -> None:
        spec = IntentSpec(name="access", route="support_agent", keywords=["sign-in"])
        intent = KeywordIntentClassifier([spec], threshold=0.1).classify(
            "after how many failed sign-in attempts am I locked out"
        )
        assert intent.name == "access"


class TestFrontLineRouting:
    def test_routes_to_the_mapped_agent(self, intents: list[IntentSpec]) -> None:
        frontline = FrontLineAgent(
            KeywordIntentClassifier(intents), {s.name: s.route for s in intents}
        )
        _, route = frontline.route("How much annual leave do I get?")
        assert route == "policy_agent"

    def test_unknown_intent_uses_the_default_route(self, intents: list[IntentSpec]) -> None:
        frontline = FrontLineAgent(
            KeywordIntentClassifier(intents),
            {s.name: s.route for s in intents},
            default_route="fallback_agent",
        )
        _, route = frontline.route("completely unrelated gibberish text")
        assert route == "fallback_agent"


class TestKnowledgeAgent:
    def _turn(self, retriever, message: str) -> Turn:
        return Turn(
            request=ChatRequest(message=message),
            llm=ExtractiveProvider(),
            retriever=retriever,
            tools=build_toolbox({"retriever": retriever}),
        )

    def test_answers_from_the_corpus_with_citations(self, retriever) -> None:
        agent = KnowledgeAgent(name="policy_agent", tools=["knowledge_search"])
        reply = agent.handle(self._turn(retriever, "How many days of annual leave do I get?"))

        assert reply.status is ResponseStatus.ANSWERED
        assert "24 days" in reply.text
        assert [c.doc_id for c in reply.citations] == ["POL-1"]
        assert reply.tool_calls[0].name == "knowledge_search"

    def test_says_it_does_not_know_when_nothing_matches(self, retriever) -> None:
        agent = KnowledgeAgent(name="policy_agent", tools=["knowledge_search"])
        reply = agent.handle(self._turn(retriever, "xylophone quokka trombone"))
        assert reply.status is ResponseStatus.NO_ANSWER

    def test_tools_outside_the_allow_list_are_blocked(self, retriever) -> None:
        # An agent with no approved tools must not be able to search at all.
        agent = KnowledgeAgent(name="policy_agent", tools=[])
        reply = agent.handle(self._turn(retriever, "How many days of annual leave?"))
        assert reply.status is ResponseStatus.NO_ANSWER
        assert reply.tool_calls[0].ok is False


class TestOtherAgents:
    def test_escalation_raises_a_ticket(self, retriever) -> None:
        agent = EscalationAgent(name="escalation_agent", tools=["create_ticket"])
        reply = agent.handle(
            Turn(
                request=ChatRequest(message="I want a human"),
                llm=ExtractiveProvider(),
                retriever=retriever,
                tools=build_toolbox({"retriever": retriever}),
            )
        )
        assert reply.status is ResponseStatus.ESCALATED
        assert reply.tool_calls[0].name == "create_ticket"
        assert reply.metadata["ticket_id"].startswith("TKT-")

    def test_fallback_lists_capabilities(self, retriever) -> None:
        agent = FallbackAgent(name="fallback_agent", capabilities=["alpha", "beta"])
        reply = agent.handle(
            Turn(
                request=ChatRequest(message="???"),
                llm=ExtractiveProvider(),
                retriever=retriever,
                tools=build_toolbox({"retriever": retriever}),
            )
        )
        assert reply.status is ResponseStatus.NO_ANSWER
        assert "alpha" in reply.text


class TestCitationExtraction:
    def test_only_retrieved_documents_become_citations(self) -> None:
        chunks = [RetrievedChunk(doc_id="A", chunk_id="A#0", text="body of A")]
        citations = extract_citations("Fact one [A] and invented [ZZZ].", chunks)
        assert [c.doc_id for c in citations] == ["A"]

    def test_duplicates_are_collapsed(self) -> None:
        chunks = [RetrievedChunk(doc_id="A", chunk_id="A#0", text="body")]
        assert len(extract_citations("One [A]. Two [A].", chunks)) == 1

    def test_citation_carries_provenance(self) -> None:
        chunks = [
            RetrievedChunk(doc_id="A", chunk_id="A#0", text="body", title="T", source="s.pdf")
        ]
        citation: Citation = extract_citations("Fact [A]", chunks)[0]
        assert (citation.title, citation.source) == ("T", "s.pdf")
