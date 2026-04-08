"""retriever_node: run semantic search and populate retrieved_objects in state."""
from __future__ import annotations

import asyncio

import structlog

from autotext2sql.agent.state import AgentState
from autotext2sql.retriever.models import RetrieverInput
from autotext2sql.retriever.search import Retriever

logger = structlog.get_logger(__name__)


def _expand_entities_for_demo_domain(query: str, entities: list[str]) -> list[str]:
    normalized = query.lower().replace("ё", "е")
    expanded = list(entities)
    expansions = {
        "брони": ["bookings", "booking", "book_ref", "book_date", "total_amount"],
        "брон": ["bookings", "booking", "book_ref", "book_date", "total_amount"],
        "билет": ["tickets", "ticket", "ticket_no", "passenger"],
        "рейс": ["flights", "flight", "flight_id", "flight_no", "scheduled_departure", "status"],
        "аэропорт": ["airports", "airport", "airport_code", "city"],
        "самолет": ["aircrafts", "aircraft", "aircraft_code", "model"],
        "мест": ["seats", "seat_no", "boarding_passes"],
        "посад": ["boarding_passes", "boarding_no", "seat_no"],
    }
    for marker, terms in expansions.items():
        if marker in normalized:
            expanded.extend(terms)
    return list(dict.fromkeys(expanded))


def make_retriever_node(retriever: Retriever):
    def retriever_node(state: AgentState) -> AgentState:
        query = state.get("user_query", "")
        entities = _expand_entities_for_demo_domain(query, state.get("extracted_entities", []))
        db_hint = state.get("db_hint")
        db_url = state.get("db_url")
        db_urls = state.get("db_urls")
        selected_databases = state.get("selected_databases")
        step_costs: list[float] = list(state.get("step_costs", []))

        inp = RetrieverInput(
            query=query,
            entities=entities,
            db_hint=db_hint,
            db_url=db_url,
            db_urls=db_urls,
            selected_databases=selected_databases,
        )
        output = retriever.retrieve(inp)

        if output.cost_usd:
            step_costs.append(output.cost_usd)

        objects_dicts = [obj.model_dump() for obj in output.objects]
        scores = [obj.relevance_score for obj in output.objects]

        logger.info(
            "retriever_node_done",
            objects=len(objects_dicts),
            confidence=output.confidence,
        )
        return {
            **state,
            "retrieved_objects": objects_dicts,
            "relevance_scores": scores,
            "db_urls": output.db_urls,
            "step_costs": step_costs,
            "total_cost": sum(step_costs),
        }

    return retriever_node
