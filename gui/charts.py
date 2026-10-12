"""Chart and visualization helpers (plotly optional, streamlit required)."""

from __future__ import annotations

from typing import Any


def _plotly_available() -> bool:
    try:
        import plotly  # noqa: F401

        return True
    except ImportError:
        return False


def bar_chart(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    title: str = "",
) -> Any | None:
    """Return a plotly bar chart figure, or None if plotly is unavailable."""
    if not _plotly_available():
        return None
    import plotly.express as px

    return px.bar(data, x=x, y=y, title=title)


def line_chart(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    title: str = "",
) -> Any | None:
    if not _plotly_available():
        return None
    import plotly.express as px

    return px.line(data, x=x, y=y, title=title)


def scatter_chart(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    title: str = "",
) -> Any | None:
    if not _plotly_available():
        return None
    import plotly.express as px

    return px.scatter(data, x=x, y=y, title=title)


def pie_chart(
    data: list[dict[str, Any]],
    names: str,
    values: str,
    title: str = "",
) -> Any | None:
    if not _plotly_available():
        return None
    import plotly.express as px

    return px.pie(data, names=names, values=values, title=title)


def st_bar_chart(st_obj: Any, data: list[dict[str, Any]], key: str = "") -> None:
    """Render a simple bar chart via streamlit (always uses streamlit chart)."""
    if not _streamlit_available():
        return
    import streamlit as st

    formatted = {d.get("x"): d.get("y", 0) for d in data if "x" in d}
    if formatted:
        st.bar_chart(formatted, key=key)


def phase_timeline_chart(sessions: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate session counts per phase for dashboard timeline."""
    counts: dict[str, int] = {}
    for s in sessions:
        phase = s.get("phase", "unknown")
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def model_comparison_data(experiments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize experiment records for model comparison table."""
    return [
        {
            "experiment_id": e.get("id"),
            "model_id": e.get("model_id") or "N/A",
            "title": e.get("title", ""),
            "status": e.get("status", ""),
            "sessions": e.get("session_count", 0),
            "turns": e.get("turn_count", 0),
        }
        for e in experiments
    ]


def _streamlit_available() -> bool:
    try:
        import streamlit  # noqa: F401

        return True
    except ImportError:
        return False


__all__ = [
    "bar_chart",
    "line_chart",
    "model_comparison_data",
    "phase_timeline_chart",
    "pie_chart",
    "scatter_chart",
    "st_bar_chart",
]

