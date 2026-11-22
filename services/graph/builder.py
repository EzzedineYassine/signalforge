from collections.abc import Sequence

import networkx as nx

from services.graph.relationships import ServiceEdge, ServiceEdgeInput
from services.ingestion.normalizer import normalize_service_name


def build_service_dependency_graph(
    edges: Sequence[ServiceEdgeInput],
    services: Sequence[str] | None = None,
) -> nx.DiGraph:
    """Build a directed service dependency graph (caller -> callee).

    - Nodes are canonical normalized service names.
    - Self-loops (caller == callee) are excluded from edges while preserving the node.
    - Duplicate (caller, callee) edge observations aggregate call_count (sum),
      error_count (sum), error_rate, and avg_duration_ms (weighted by call_count).
    """
    graph = nx.DiGraph()

    if services is not None:
        for raw_service in services:
            graph.add_node(normalize_service_name(raw_service))

    # Map (caller, callee) -> [total_calls, total_errors, weighted_duration_sum]
    aggregates: dict[tuple[str, str], list[float]] = {}

    for edge in edges:
        caller = normalize_service_name(edge.caller)
        callee = normalize_service_name(edge.callee)
        graph.add_node(caller)
        graph.add_node(callee)

        if caller == callee:
            continue

        key = (caller, callee)
        if key not in aggregates:
            aggregates[key] = [0.0, 0.0, 0.0]

        aggregates[key][0] += float(edge.call_count)
        aggregates[key][1] += float(edge.error_count)
        aggregates[key][2] += float(edge.avg_duration_ms) * float(edge.call_count)

    for (caller, callee), (calls_f, errors_f, weighted_dur) in sorted(aggregates.items()):
        total_calls = int(calls_f)
        total_errors = int(errors_f)
        avg_duration_ms = weighted_dur / total_calls
        error_rate = total_errors / total_calls
        graph.add_edge(
            caller,
            callee,
            call_count=total_calls,
            error_count=total_errors,
            error_rate=error_rate,
            avg_duration_ms=avg_duration_ms,
        )

    return graph


def extract_service_nodes(graph: nx.DiGraph) -> list[str]:
    """Return sorted service node names from a dependency graph."""
    return sorted(str(node) for node in graph.nodes)


def extract_service_edges(graph: nx.DiGraph) -> list[ServiceEdge]:
    """Return deterministically sorted ServiceEdge models from a dependency graph."""
    edges: list[ServiceEdge] = []
    for caller, callee, attrs in sorted(
        graph.edges(data=True),
        key=lambda item: (str(item[0]), str(item[1])),
    ):
        call_count = int(attrs.get("call_count", 1))
        error_count = int(attrs.get("error_count", 0))
        error_rate = float(attrs.get("error_rate", error_count / call_count))
        avg_duration_ms = float(attrs.get("avg_duration_ms", 0.0))
        edges.append(
            ServiceEdge(
                caller=str(caller),
                callee=str(callee),
                call_count=call_count,
                error_count=error_count,
                error_rate=error_rate,
                avg_duration_ms=avg_duration_ms,
            )
        )
    return edges
