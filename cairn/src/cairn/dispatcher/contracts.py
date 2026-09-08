from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact")) and (
        "complete" not in payload or _is_dict(payload.get("complete"))
    )


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) == {"description"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_parent_reason_payload(
    payload: dict[str, Any], *, open_intents_empty: bool, max_intents: int
) -> dict[str, Any]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return {"kind": "rejected"}
    if accepted is None:
        data = payload
    if not isinstance(data, dict):
        raise ValueError("parent plan data must be an object")
    allowed = {"complete", "intents", "moves", "hints"}
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"unexpected parent plan keys: {sorted(extra)}")
    complete = data.get("complete")
    intents = data.get("intents", [])
    moves = data.get("moves", [])
    hints = data.get("hints", [])
    if complete is not None:
        if any((intents, moves, hints)):
            raise ValueError("complete cannot coexist with other parent actions")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        evidence = complete.get("evidence")
        if evidence is not None:
            required = {"kind", "value", "source", "verified"}
            if not isinstance(evidence, dict) or not required.issubset(evidence):
                raise ValueError("invalid completion evidence")
            if evidence["kind"] not in {"flag", "proof"} or not isinstance(evidence["verified"], bool):
                raise ValueError("invalid completion evidence fields")
    if not isinstance(intents, list) or not isinstance(moves, list) or not isinstance(hints, list):
        raise ValueError("intents, moves, and hints must be arrays")
    for index, intent in enumerate(intents[:max_intents]):
        required = {"from", "title", "origin", "goal"}
        if not isinstance(intent, dict) or not required.issubset(intent):
            raise ValueError(f"invalid Child intent at index {index}")
        if not isinstance(intent["from"], list) or not intent["from"]:
            raise ValueError(f"Child intent from must be non-empty at index {index}")
    for index, move in enumerate(moves):
        required = {"agent_id", "target_child_id", "reason"}
        if not isinstance(move, dict) or not required.issubset(move):
            raise ValueError(f"invalid move at index {index}")
    for index, hint in enumerate(hints):
        if not isinstance(hint, dict) or not {"child_id", "content"}.issubset(hint):
            raise ValueError(f"invalid hint at index {index}")
    if complete is None and open_intents_empty and not intents:
        raise ValueError("Parent must create at least one Child when no open intents exist")
    return {
        "kind": "complete" if complete is not None else "plan",
        "complete": complete,
        "intents": intents[:max_intents],
        "moves": moves,
        "hints": hints,
    }


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        return "fact", result
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()
