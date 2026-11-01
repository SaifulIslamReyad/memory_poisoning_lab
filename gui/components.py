"""Streamlit UI components (Streamlit is optional at import time)."""

from __future__ import annotations

from typing import Any


def _streamlit_available() -> bool:
    try:
        import streamlit  # noqa: F401

        return True
    except ImportError:
        return False


def model_selector(
    models: list[dict[str, Any]],
    key: str = "model_select",
    label: str = "Select model",
) -> str | None:
    """Render a model dropdown; returns None when streamlit is unavailable."""
    if not _streamlit_available():
        return None
    import streamlit as st

    ids = [m["id"] for m in models]
    if not ids:
        return None
    return st.selectbox(label, ids, key=key)


def text_input(
    label: str,
    value: str = "",
    key: str | None = None,
) -> str:
    """Render a text input; returns empty string when streamlit is unavailable."""
    if not _streamlit_available():
        return value
    import streamlit as st

    return st.text_input(label, value=value, key=key)


def text_area(
    label: str,
    value: str = "",
    key: str | None = None,
) -> str:
    if not _streamlit_available():
        return value
    import streamlit as st

    return st.text_area(label, value=value, key=key)


def info(message: str) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    st.info(message)


def warning(message: str) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    st.warning(message)


def error(message: str) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    st.error(message)


def success(message: str) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    st.success(message)


def write_json(data: dict[str, Any], key: str | None = None) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    import json

    st.json(json.dumps(data, indent=2, default=str), key=key)


def data_table(data: list[dict[str, Any]]) -> None:
    if not _streamlit_available():
        return
    import streamlit as st

    if data:
        st.dataframe(data, width='stretch')


def spinner(message: str) -> Any:
    if not _streamlit_available():
        return None
    import streamlit as st

    return st.spinner(message)


def checkbox(label: str, value: bool = False, key: str | None = None) -> bool:
    if not _streamlit_available():
        return value
    import streamlit as st

    return st.checkbox(label, value=value, key=key)


def button(label: str, key: str | None = None) -> bool:
    if not _streamlit_available():
        return False
    import streamlit as st

    return st.button(label, key=key)


def slider(
    label: str,
    min_value: float = 0.0,
    max_value: float = 1.0,
    value: float = 0.0,
    key: str | None = None,
) -> float:
    if not _streamlit_available():
        return value
    import streamlit as st

    return st.slider(label, min_value, max_value, value, key=key)
