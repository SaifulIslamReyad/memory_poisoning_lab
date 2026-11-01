"""Streamlit page renderers for catalog, chat, dashboard, and archive."""

from __future__ import annotations

import json
from typing import Any


def _streamlit_available() -> bool:
    try:
        import streamlit  # noqa: F401

        return True
    except ImportError:
        return False


def _get_lab_connection(config: Any | None = None, *, readonly: bool = False):
    from core.config import load_config
    from core.database import connect_database

    cfg = config or load_config()
    return connect_database(cfg.database_target, readonly=readonly)


def _get_model_capabilities(models: list[dict[str, Any]], model_id: str | None):
    from core.models import ModelCapabilities

    if not model_id:
        return None
    for m in models:
        if m.get("id") == model_id:
            return ModelCapabilities.from_dict(m)
    return None


def _load_transcript_turns(session_id: str) -> list[dict[str, Any]]:
    """Load transcript turns from SQLite for a session."""
    from core.config import load_config, redact
    from core.experiments import get_turns

    config = load_config()
    conn = _get_lab_connection(config, readonly=True)
    try:
        turns = get_turns(conn, session_id=session_id)
        messages = []
        for t in turns:
            if t.get("role") not in ("user", "assistant"):
                continue
            content = t.get("content")
            if content is None and t.get("error"):
                content = f"Error: {t['error']}"
            if content is None:
                continue
            messages.append(
                {
                    "role": t["role"],
                    "content": str(content),
                    "turn_id": t["id"],
                    "raw_request": redact(t.get("model_request")),
                    "raw_response": redact(t.get("model_response")),
                }
            )
        return messages
    finally:
        conn.close()


def _run_chat_turn(
    config: Any,
    experiment_id: str,
    session_id: str,
    prompt: str,
    role: str,
    system_prompt: str,
    model_id: str,
    parameters: dict[str, Any],
    phase: str,
    tool_names: list[str] | None,
    tool_definitions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Run one model turn in a background worker."""
    from core.agent import AgentRunner

    runner = AgentRunner(config=config, system_prompt=system_prompt)
    return runner.run_turn(
        experiment_id=experiment_id,
        session_id=session_id,
        content=prompt,
        role=role,
        model_id=model_id,
        parameters=parameters or None,
        phase=phase,
        tool_names=tool_names,
        tool_definitions=tool_definitions,
    )


def _safe_error(exc: Exception) -> str:
    from core.config import redact

    return redact(f"{type(exc).__name__}: {exc}")


def _code_block(value: Any) -> None:
    import streamlit as st

    st.code(str(value), language="text")


def _json_code(value: Any) -> None:
    import json
    import streamlit as st
    from core.config import redact

    _code_block(json.dumps(redact(value), indent=2, default=str, sort_keys=True))


# ---------------------------------------------------------------------------
# Catalog Page
# ---------------------------------------------------------------------------


def render_catalog_page(
    models: list[dict[str, Any]] | None = None,
    stats: dict[str, int] | None = None,
) -> None:
    """Render the model catalog page."""
    if not _streamlit_available():
        return
    import streamlit as st

    from core.catalog import import_model_snapshot, list_models, sync_models
    from core.config import load_config

    st.title("Model Catalog")

    col_sync, col_import = st.columns(2)
    with col_sync:
        if st.button("Sync Models", key="sync_models_btn"):
            config = load_config()
            api_key = config.api_key
            if not api_key:
                st.error("GROQ_API_KEY is required for syncing.")
            else:
                with st.spinner("Syncing models from Groq..."):
                    try:
                        result = sync_models(
                            api_key,
                            db_path=config.models_target,
                            timeout=config.request_timeout,
                            url=f"{config.groq_base_url.rstrip('/')}/models",
                        )
                        st.success(
                            f"Synced {result['imported']} models. "
                            f"{len(result['warnings'])} warnings."
                        )
                        if result["warnings"]:
                            for w in result["warnings"]:
                                st.warning(w)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Sync failed: {_safe_error(exc)}")
    with col_import:
        if st.button("Import Snapshot", key="import_snapshot_btn"):
            config = load_config()
            with st.spinner("Importing model snapshot..."):
                try:
                    result = import_model_snapshot(
                        config.model_snapshot, db_path=config.models_target
                    )
                    st.success(
                        f"Imported {result['imported']} models from snapshot. "
                        f"{len(result['warnings'])} warnings."
                    )
                    if result["warnings"]:
                        for w in result["warnings"]:
                            st.warning(w)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Import failed: {_safe_error(exc)}")

    if stats:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total", stats.get("total", 0))
        c2.metric("Active", stats.get("active", 0))
        c3.metric("Features", stats.get("features", 0))
        c4.metric("Parameters", stats.get("parameters", 0))

    filter_col, search_col, active_col, modality_col = st.columns(4)
    with filter_col:
        search = st.text_input(
            "Search models",
            key="catalog_search",
            placeholder="Search by ID, name, or owner",
        )
    with active_col:
        active_only = st.checkbox("Active only", value=True, key="catalog_active")
    with modality_col:
        modality = st.selectbox(
            "Modality",
            ["all", "text", "image", "audio", "speech", "transcription"],
            key="catalog_modality",
        )
    if modality == "all":
        modality_filter = None
    else:
        modality_filter = modality

    if models is None:
        models = []

    filtered = models
    if search:
        term = search.lower()
        filtered = [
            m
            for m in filtered
            if term in (m.get("id") or "").lower()
            or term in (m.get("name") or "").lower()
            or term in (m.get("owned_by") or "").lower()
        ]
    if active_only:
        filtered = [m for m in filtered if m.get("active")]
    if modality_filter:
        filtered = [
            m
            for m in filtered
            if modality_filter in (m.get("input_modalities") or [])
            or modality_filter in (m.get("output_modalities") or [])
        ]

    if filtered:
        st.dataframe(
            [
                {
                    "id": m["id"],
                    "name": m.get("name") or "N/A",
                    "owned_by": m.get("owned_by") or "N/A",
                    "active": m.get("active"),
                    "context_window": m.get("context_window") or "N/A",
                }
                for m in filtered
            ],
            width='stretch',
            key="catalog_table",
        )
    else:
        st.info("No models match the current filters.")

    st.subheader("Model Detail")
    model_ids = [m["id"] for m in filtered]
    if model_ids:
        selected_id = st.selectbox(
            "Select model for detail", model_ids, key="catalog_detail_select"
        )
        if selected_id:
            from core.catalog import get_model
            from core.config import load_config

            config = load_config()
            detail = get_model(config.models_target, selected_id)
            if detail:
                detail_col1, detail_col2 = st.columns(2)
                with detail_col1:
                    st.write("ID:", detail.get("id", "N/A"))
                    st.write("Name:", detail.get("name") or "N/A")
                    st.write("Object:", detail.get("object") or "N/A")
                    created = detail.get("created")
                    if created:
                        from datetime import datetime, timezone

                        st.write(
                            "Created:",
                            datetime.fromtimestamp(
                                created, tz=timezone.utc
                            ).isoformat(),
                        )
                    st.write("Owned By:", detail.get("owned_by") or "N/A")
                    st.write("Active:", detail.get("active"))
                    st.write("Context Window:", detail.get("context_window") or "N/A")
                    st.write("Context Length:", detail.get("context_length") or "N/A")
                with detail_col2:
                    st.write(
                        "Max Completion Tokens:",
                        detail.get("max_completion_tokens") or "N/A",
                    )
                    st.write(
                        "Max Output Length:", detail.get("max_output_length") or "N/A"
                    )
                    st.write("Hugging Face ID:", detail.get("hugging_face_id") or "N/A")
                    st.write(
                        "Input Modalities:",
                        ", ".join(detail.get("input_modalities") or []) or "N/A",
                    )
                    st.write(
                        "Output Modalities:",
                        ", ".join(detail.get("output_modalities") or []) or "N/A",
                    )

                with st.expander("Pricing", expanded=False):
                    pricing = detail.get("pricing") or {}
                    if pricing:
                        st.dataframe(
                            [{"metric": k, "price": v} for k, v in pricing.items()],
                            width='stretch',
                        )
                    else:
                        st.info("No pricing information.")

                with st.expander("Supported Features", expanded=False):
                    features = detail.get("features") or []
                    if features:
                        st.write(", ".join(features))
                    else:
                        st.info("No features listed.")

                with st.expander("Sampling Parameters", expanded=False):
                    params = detail.get("sampling_parameters") or []
                    if params:
                        st.write(", ".join(params))
                    else:
                        st.info("No sampling parameters listed.")

                with st.expander("Raw JSON", expanded=False):
                    raw = detail.get("raw_json")
                    if raw:
                        st.json(raw if isinstance(raw, dict) else json.loads(raw))
                    else:
                        st.info("No raw JSON available.")
            else:
                st.warning("Model detail not found.")
    else:
        st.info("No models available for detail view.")


# ---------------------------------------------------------------------------
# Chat Console Page
# ---------------------------------------------------------------------------


def render_chat_page(
    models: list[dict[str, Any]] | None = None,
    experiment_ids: list[str] | None = None,
) -> None:
    """Render the chat/console page."""
    if not _streamlit_available():
        return
    import streamlit as st

    from core.experiments import (
        PHASES,
        create_experiment,
        create_session,
        delete_experiment,
        delete_session,
        finish_experiment,
        get_session,
        get_turns,
        list_sessions,
        transition_session,
    )
    from core.memory import (
        delete_memory,
        list_memory_events,
        quarantine_memory,
        read_memory,
        reset_memory,
        write_memory,
    )
    from core.detections import run_detectors
    from core.mitigations import STRATEGIES, apply_mitigation
    from core.config import load_config
    from core.models import validate_request

    st.title("Chat Console")

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    if "chat_experiment_id" not in st.session_state:
        st.session_state.chat_experiment_id = None
    if "chat_session_id" not in st.session_state:
        st.session_state.chat_session_id = None
    if "chat_transcript_session_id" not in st.session_state:
        st.session_state.chat_transcript_session_id = None
    if "chat_job_id" not in st.session_state:
        st.session_state.chat_job_id = None

    model_ids = [m["id"] for m in (models or [])]
    text_model_ids = [
        m["id"]
        for m in (models or [])
        if "text" in (m.get("input_modalities") or [])
        and "text" in (m.get("output_modalities") or [])
    ]

    if text_model_ids:
        selected_model = st.selectbox("Model", text_model_ids, key="chat_model_select")
    elif model_ids:
        selected_model = st.selectbox(
            "Model (no text/text)", model_ids, key="chat_model_select"
        )
    else:
        selected_model = None
        st.warning("No models available. Sync or import the catalog first.")

    config = load_config()
    session: dict[str, Any] | None = None
    selected_session_id: str | None = None

    st.subheader("Experiment & Session")
    exp_col, sess_col = st.columns(2)
    with exp_col:
        current_exp = st.session_state.chat_experiment_id
        exp_options = list(
            dict.fromkeys(
                [
                    *(experiment_ids or []),
                    current_exp if current_exp in (experiment_ids or []) else None,
                ]
            )
        )
        exp_options = [item for item in exp_options if item]
        new_exp_name = st.text_input(
            "New experiment name",
            key="new_experiment_name",
            placeholder="Enter name to create a new experiment",
        )
        poison_vector = st.selectbox(
            "Poison vector",
            [
                "none",
                "direct_assertion",
                "retrieved_document",
                "tool_response",
                "assistant_summary",
                "persisted_profile",
            ],
            key="new_poison_vector",
        )
        mitigation_strategy = st.selectbox(
            "Mitigation strategy",
            ["none", *sorted(STRATEGIES)],
            key="new_mitigation_strategy",
        )
        if exp_options:
            selected_exp = st.selectbox(
                "Select experiment",
                exp_options,
                index=(
                    exp_options.index(current_exp) if current_exp in exp_options else 0
                ),
                key="chat_experiment_select",
            )
        else:
            selected_exp = None
        if st.button("Create Experiment", key="create_exp_btn"):
            if not selected_model:
                st.warning("Select a model before creating an experiment.")
            elif new_exp_name.strip():
                conn = _get_lab_connection(config)
                try:
                    exp_id = create_experiment(
                        conn,
                        new_exp_name.strip(),
                        model_id=selected_model,
                        poison_vector=(
                            None if poison_vector == "none" else poison_vector
                        ),
                        mitigation_strategy=(
                            None
                            if mitigation_strategy == "none"
                            else mitigation_strategy
                        ),
                    )

                    st.session_state.chat_experiment_id = exp_id
                    st.session_state.chat_session_id = None
                    st.session_state.chat_transcript_session_id = None
                    st.session_state.chat_messages = []
                    st.success(f"Created experiment: {exp_id}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to create experiment: {_safe_error(exc)}")
                finally:
                    conn.close()
            else:
                st.warning("Experiment name is required.")

        # Delete Experiment button
        if exp_options and selected_exp:
            if st.button("Delete Experiment", key="delete_exp_btn", type="secondary"):
                conn = _get_lab_connection(config)
                try:
                    deleted = delete_experiment(conn, selected_exp)
                    if deleted:
                        if st.session_state.chat_experiment_id == selected_exp:
                            st.session_state.chat_experiment_id = None
                            st.session_state.chat_session_id = None
                            st.session_state.chat_transcript_session_id = None
                            st.session_state.chat_messages = []
                        st.success(f"Deleted experiment: {selected_exp}")
                        st.rerun()
                    else:
                        st.error("Experiment not found.")
                except Exception as exc:
                    st.error(f"Failed to delete experiment: {_safe_error(exc)}")
                finally:
                    conn.close()

        if selected_exp != st.session_state.chat_experiment_id:
            st.session_state.chat_experiment_id = selected_exp
            st.session_state.chat_session_id = None
            st.session_state.chat_transcript_session_id = None
            st.session_state.chat_messages = []

    with sess_col:
        if st.session_state.chat_experiment_id:
            conn = _get_lab_connection(config, readonly=True)
            try:
                sessions = list_sessions(
                    conn, experiment_id=st.session_state.chat_experiment_id
                )
            finally:
                conn.close()
            sess_options = [s["id"] for s in sessions]
            if sess_options:
                current_sess = st.session_state.chat_session_id
                selected_session_id = st.selectbox(
                    "Select session",
                    sess_options,
                    index=(
                        sess_options.index(current_sess)
                        if current_sess in sess_options
                        else 0
                    ),
                    key="chat_session_select",
                )
                if selected_session_id != st.session_state.chat_session_id:
                    st.session_state.chat_session_id = selected_session_id
                    st.session_state.chat_transcript_session_id = None
                    st.session_state.chat_messages = []
            else:
                selected_session_id = None
                st.session_state.chat_session_id = None
                st.session_state.chat_transcript_session_id = None
                st.session_state.chat_messages = []
                st.info("No sessions yet. Create one below.")
        else:
            selected_session_id = None
            st.session_state.chat_session_id = None
            st.session_state.chat_transcript_session_id = None
            st.session_state.chat_messages = []
            st.info("Create an experiment first.")

        new_session_phase = st.selectbox(
            "New session phase",
            sorted(PHASES),
            key="new_session_phase",
        )
        new_namespace = (
            st.text_input(
                "Memory namespace (blank reuses run namespace)",
                key="new_memory_namespace",
                placeholder="optional",
            ).strip()
            or None
        )
        if st.button("Create Session", key="create_sess_btn"):
            if st.session_state.chat_experiment_id:
                conn = _get_lab_connection(config)
                try:
                    sess_id = create_session(
                        conn,
                        st.session_state.chat_experiment_id,
                        phase=new_session_phase,
                        memory_namespace=new_namespace,
                    )
                    st.session_state.chat_session_id = sess_id
                    st.session_state.chat_transcript_session_id = None
                    st.session_state.chat_messages = []
                    st.success(f"Created session: {sess_id}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to create session: {_safe_error(exc)}")
                finally:
                    conn.close()
            else:
                st.warning("Create an experiment first.")

        # Delete Session button
        if selected_session_id:
            if st.button("Delete Session", key="delete_sess_btn", type="secondary"):
                conn = _get_lab_connection(config)
                try:
                    deleted = delete_session(conn, selected_session_id)
                    if deleted:
                        if st.session_state.chat_session_id == selected_session_id:
                            st.session_state.chat_session_id = None
                            st.session_state.chat_transcript_session_id = None
                            st.session_state.chat_messages = []
                        st.success(f"Deleted session: {selected_session_id}")
                        st.rerun()
                    else:
                        st.error("Session not found.")
                except Exception as exc:
                    st.error(f"Failed to delete session: {_safe_error(exc)}")
                finally:
                    conn.close()

    if selected_session_id:
        conn = _get_lab_connection(config, readonly=True)
        try:
            session = get_session(conn, selected_session_id)
            if (
                session
                and session.get("experiment_id") != st.session_state.chat_experiment_id
            ):
                session = None
        finally:
            conn.close()
        if session is None:
            st.session_state.chat_session_id = None
            st.session_state.chat_transcript_session_id = None
            st.session_state.chat_messages = []
            selected_session_id = None
            st.warning("The selected session is no longer available.")
        elif st.session_state.chat_transcript_session_id != selected_session_id:
            st.session_state.chat_messages = _load_transcript_turns(selected_session_id)
            st.session_state.chat_transcript_session_id = selected_session_id

    st.subheader("Chat Transcript")
    job_id = st.session_state.get("chat_job_id")
    if job_id:
        from core.workers import get_job

        job = get_job(job_id)
        if job is None:
            st.session_state.chat_job_id = None
        elif job.get("status") == "completed":
            result = job.get("result") or {}
            st.session_state.chat_messages.append(
                {
                    "role": "assistant",
                    "content": str(result.get("content", "")),
                    "turn_id": result.get("turn_id"),
                    "raw_request": result.get("request"),
                    "raw_response": result.get("response"),
                    "detections": result.get("detections", []),
                }
            )
            st.session_state.chat_job_id = None
            st.rerun()
        elif job.get("status") == "failed":
            error = job.get("error") or "background turn failed"
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": f"Error: {error}"}
            )
            st.session_state.chat_job_id = None
            st.rerun()
        elif job.get("status") == "cancelled":
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": "Error: turn cancelled"}
            )
            st.session_state.chat_job_id = None
            st.rerun()
        else:
            st.info(f"Model turn is {job.get('status', 'queued')}.")
            import time

            time.sleep(1)
            st.rerun()

    messages = st.session_state.chat_messages
    for msg in messages:
        role = msg.get("role", "user")
        turn_id = msg.get("turn_id")
        content = msg.get("content", "")
        with st.chat_message(role):
            if content:
                _code_block(content)
            if turn_id:
                st.caption(f"Turn ID: {turn_id}")
            if msg.get("detections"):
                with st.expander("Detections", expanded=False):
                    for detection in msg["detections"]:
                        _json_code(detection)

    from core.agent import DEFAULT_SYSTEM_PROMPT

    system_prompt = st.text_area(
        "System prompt",
        value=DEFAULT_SYSTEM_PROMPT,
        key="chat_system_prompt",
        help="This prompt is sent as the system message for the next model turn.",
        height=140,
    )
    chat_role = st.selectbox(
        "Message role",
        ["user", "system", "assistant"],
        key="chat_role_select",
        help="Choose the role recorded for and sent with the next message.",
    )
    if prompt := st.chat_input("Your message"):
        if not config.api_key:
            st.error("GROQ_API_KEY is not configured.")
            st.info(
                "Create a local .env file with GROQ_API_KEY=your_key, "
                "or add GROQ_API_KEY to your hosting provider's secrets."
            )
            st.stop()
        if not selected_session_id or not selected_model:
            st.warning("Select a model and session to run a turn.")
        else:
            current_job_id = st.session_state.get("chat_job_id")
            if current_job_id:
                from core.workers import get_job as get_current_job

                current_job = get_current_job(current_job_id)
                if current_job and current_job.get("status") in {"queued", "running"}:
                    st.warning("A model turn is already running.")
                else:
                    st.session_state.chat_job_id = None
            if not st.session_state.chat_job_id:
                st.session_state.chat_messages.append(
                    {"role": chat_role, "content": prompt}
                )
                with st.chat_message(chat_role):
                    _code_block(prompt)

                capabilities = _get_model_capabilities(models, selected_model)
                params: dict[str, Any] = {}
                if capabilities:
                    sampling = capabilities.sampling_parameters
                    if "temperature" in sampling:
                        params["temperature"] = st.session_state.get("adv_temp", 0.7)
                    if "top_p" in sampling:
                        params["top_p"] = st.session_state.get("adv_top_p", 0.9)
                    if "stop" in sampling:
                        stop_value = str(
                            st.session_state.get("adv_stop", "") or ""
                        ).strip()
                        if stop_value:
                            params["stop"] = [
                                item.strip()
                                for item in stop_value.split(",")
                                if item.strip()
                            ][:4]
                    if "seed" in sampling:
                        params["seed"] = st.session_state.get("adv_seed", 42)
                    if "max_tokens" in sampling:
                        params["max_tokens"] = st.session_state.get(
                            "adv_max_tokens", 1024
                        )
                    if capabilities.supports("json_mode") and st.session_state.get(
                        "adv_json", False
                    ):
                        params["json_mode"] = True
                    if (
                        capabilities.supports("reasoning")
                        and st.session_state.get("adv_reasoning", "none") != "none"
                    ):
                        params["reasoning_effort"] = st.session_state.get(
                            "adv_reasoning", "medium"
                        )
                    structured_schema = str(
                        st.session_state.get("adv_structured_schema", "") or ""
                    ).strip()
                    if structured_schema and (
                        capabilities.supports("structured_outputs")
                        or capabilities.supports("json_mode")
                    ):
                        try:
                            params["response_format"] = {
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "lab_structured_output",
                                    "schema": json.loads(structured_schema),
                                    "strict": True,
                                },
                            }
                        except ValueError as exc:
                            params["invalid_parameter"] = (
                                f"structured-output schema must be valid JSON: {exc}"
                            )
                    tool_definitions_json = str(
                        st.session_state.get("adv_tool_definitions", "") or ""
                    ).strip()
                    if tool_definitions_json and capabilities.supports("tools"):
                        try:
                            parsed_tools = json.loads(tool_definitions_json)
                            if not isinstance(parsed_tools, list) or not parsed_tools:
                                raise ValueError(
                                    "tool definitions must be a non-empty list"
                                )
                            params["tools"] = parsed_tools
                            tool_definitions = parsed_tools
                            tool_names = [
                                item.get("function", {}).get("name")
                                for item in parsed_tools
                                if isinstance(item, dict)
                                and isinstance(item.get("function"), dict)
                                and item["function"].get("name")
                            ]
                            if not tool_names or any(
                                not isinstance(name, str) for name in tool_names
                            ):
                                raise ValueError(
                                    "each tool definition must contain function.name"
                                )
                            unsupported_tools = set(tool_names) - {"read_only_sql"}
                            if unsupported_tools:
                                raise ValueError(
                                    "unregistered tools are not executable: "
                                    + ", ".join(sorted(unsupported_tools))
                                )
                        except (TypeError, ValueError) as exc:
                            params["invalid_parameter"] = (
                                f"tool definitions must be valid JSON: {exc}"
                            )
                try:
                    if capabilities is None:
                        raise ValueError("model capabilities are unavailable")
                    from core.models import validate_request

                    validate_request(capabilities, params)
                except Exception as exc:
                    st.error(f"Invalid model parameters: {_safe_error(exc)}")
                else:
                    experiment_id = str(st.session_state.chat_experiment_id or "")
                    tool_names = tool_names if "tool_names" in locals() else None
                    tool_definitions = (
                        tool_definitions if "tool_definitions" in locals() else None
                    )
                    if capabilities.supports("tools") and not tool_names:
                        tool_names = ["read_only_sql"]
                        tool_definitions = [
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_only_sql",
                                    "description": "Approved read-only laboratory tool",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "sql": {
                                                "type": "string",
                                                "description": "One read-only SELECT query",
                                            }
                                        },
                                        "required": ["sql"],
                                        "additionalProperties": False,
                                    },
                                },
                            }
                        ]
                    from core.workers import submit_job

                    job_id = submit_job(
                        "chat-turn",
                        _run_chat_turn,
                        config,
                        experiment_id,
                        selected_session_id,
                        prompt,
                        chat_role,
                        system_prompt,
                        selected_model,
                        params,
                        session["phase"] if session else "baseline",
                        tool_names,
                        tool_definitions,
                    )
                    st.session_state.chat_job_id = job_id
                    st.info("Model turn submitted to the background worker.")
                    st.rerun()

    capabilities = _get_model_capabilities(models, selected_model)
    st.subheader("Advanced Parameters")
    with st.expander("Parameter Controls", expanded=False):
        if capabilities:
            st.write(
                f"Capabilities: {capabilities.model_id} | "
                f"Modalities: {capabilities.input_modalities}/{capabilities.output_modalities} | "
                f"Features: {', '.join(sorted(capabilities.features))}"
            )
        sampling = capabilities.sampling_parameters if capabilities else frozenset()
        temp_enabled = "temperature" in sampling
        top_p_enabled = "top_p" in sampling
        stop_enabled = "stop" in sampling
        seed_enabled = "seed" in sampling
        max_tokens_enabled = "max_tokens" in sampling
        st.slider(
            "Temperature" if temp_enabled else "Temperature (not advertised)",
            0.0,
            2.0,
            0.7,
            key="adv_temp",
            disabled=not temp_enabled,
        )
        st.slider(
            "Top-p" if top_p_enabled else "Top-p (not advertised)",
            0.0,
            1.0,
            0.9,
            key="adv_top_p",
            disabled=not top_p_enabled,
        )
        st.text_input(
            "Stop sequences" if stop_enabled else "Stop sequences (not advertised)",
            key="adv_stop",
            placeholder="comma-separated" if stop_enabled else "",
            disabled=not stop_enabled,
        )
        st.number_input(
            "Seed" if seed_enabled else "Seed (not advertised)",
            min_value=0,
            value=42,
            key="adv_seed",
            disabled=not seed_enabled,
        )
        max_limit = capabilities.max_completion_tokens if capabilities else None
        st.number_input(
            "Max tokens" if max_tokens_enabled else "Max tokens (not advertised)",
            min_value=1,
            max_value=max_limit,
            value=min(1024, max_limit) if max_limit else 1024,
            key="adv_max_tokens",
            disabled=not max_tokens_enabled,
        )
        json_mode = st.checkbox(
            (
                "JSON mode"
                if capabilities and capabilities.supports("json_mode")
                else "JSON mode (not supported)"
            ),
            key="adv_json",
            disabled=not (capabilities and capabilities.supports("json_mode")),
        )
        reasoning_value = st.selectbox(
            (
                "Reasoning effort"
                if capabilities and capabilities.supports("reasoning")
                else "Reasoning effort (not supported)"
            ),
            ["none", "low", "medium", "high"],
            key="adv_reasoning",
            disabled=not (capabilities and capabilities.supports("reasoning")),
        )
        structured_supported = bool(
            capabilities
            and (
                capabilities.supports("structured_outputs")
                or capabilities.supports("json_mode")
            )
        )
        st.text_area(
            (
                "Structured-output JSON schema"
                if structured_supported
                else "Structured-output JSON schema (not supported)"
            ),
            key="adv_structured_schema",
            placeholder='{"type":"object","properties":{}}',
            disabled=not structured_supported,
        )
        st.text_area(
            (
                "Tool definitions JSON"
                if capabilities and capabilities.supports("tools")
                else "Tool definitions JSON (not supported)"
            ),
            key="adv_tool_definitions",
            help=(
                "The only executable tool currently available is read_only_sql. "
                "Define its JSON schema here; custom names require a registered Python tool."
            ),
            placeholder=(
                '[{"type":"function","function":{"name":"read_only_sql",'
                '"description":"Run one read-only SQL query",'
                '"parameters":{"type":"object","properties":{"sql":'
                '{"type":"string"}},"required":["sql"]}}}]'
            ),
            disabled=not (capabilities and capabilities.supports("tools")),
        )
        if capabilities and capabilities.supports("tools"):
            st.caption(
                "Available executable tool: read_only_sql(sql). "
                "It queries the isolated experiment sandbox with read-only limits."
            )
        supported_params = {
            key: value
            for key, value in {
                "temperature": st.session_state.get("adv_temp", 0.7),
                "top_p": st.session_state.get("adv_top_p", 0.9),
                "stop": st.session_state.get("adv_stop", ""),
                "seed": st.session_state.get("adv_seed", 42),
                "max_tokens": st.session_state.get("adv_max_tokens", 1024),
                "json_mode": json_mode,
                "reasoning_effort": reasoning_value,
                "response_format": locals().get("params", {}).get("response_format"),
                "tools": locals().get("params", {}).get("tools"),
            }.items()
            if (
                (
                    key == "json_mode"
                    and capabilities
                    and capabilities.supports("json_mode")
                )
                or (
                    key == "reasoning_effort"
                    and capabilities
                    and capabilities.supports("reasoning")
                )
                or (key in {"response_format", "tools"} and value is not None)
                or key in sampling
            )
        }
        st.session_state.adv_params = supported_params
        _json_code(supported_params)

    show_raw = st.checkbox(
        "Show raw evidence (request/response)", key="raw_evidence_toggle"
    )
    if show_raw:
        for msg in reversed(st.session_state.chat_messages):
            if msg.get("raw_request"):
                with st.expander(
                    f"Raw Request - {msg.get('turn_id', 'unknown')}", expanded=False
                ):
                    _json_code(msg["raw_request"])
            if msg.get("raw_response"):
                with st.expander(
                    f"Raw Response - {msg.get('turn_id', 'unknown')}", expanded=False
                ):
                    _json_code(msg["raw_response"])

    st.subheader("Memory Panel")
    if selected_session_id and session:
        namespace = session["memory_namespace"]
        st.markdown(f"**Memory Namespace:** `{namespace}`")

        mem_col1, mem_col2, mem_col3, mem_col4, mem_col5 = st.columns(5)
        with mem_col1:
            if st.button("Read Memory", key="mem_read_btn"):
                conn = _get_lab_connection(config, readonly=True)
                try:
                    entries = read_memory(conn, namespace, policy="all")
                    if entries:
                        with st.expander("Memory Entries", expanded=True):
                            st.dataframe(
                                [
                                    {
                                        "key": e.get("memory_key"),
                                        "value": (
                                            json.dumps(e.get("value"))[:100]
                                            if e.get("value")
                                            else None
                                        ),
                                        "provenance": e.get("provenance", "N/A"),
                                        "status": e.get("status", "N/A"),
                                    }
                                    for e in entries
                                ],
            width='stretch',
        )
                    else:
                        st.info("No memory entries.")
                finally:
                    conn.close()
        with mem_col2:
            mem_key = st.text_input("Memory key", key="mem_key", placeholder="key")
            mem_value = st.text_area(
                "Memory value", key="mem_value", placeholder="value"
            )
            mem_provenance = st.selectbox(
                "Provenance",
                ["explicit", "trusted", "untrusted", "poison", "tool"],
                key="mem_provenance",
            )
            if st.button("Write Memory", key="mem_write_btn"):
                if mem_key.strip():
                    conn = _get_lab_connection(config)
                    try:
                        write_memory(
                            conn,
                            selected_session_id,
                            mem_key.strip(),
                            mem_value or "",
                            provenance=mem_provenance,
                        )
                        st.success("Memory written.")
                    except Exception as exc:
                        st.error(f"Write failed: {_safe_error(exc)}")
                    finally:
                        conn.close()
                else:
                    st.warning("Memory key is required.")
            if st.button("Write Poison Memory", key="write_poison_memory_btn"):
                if mem_key.strip():
                    conn = _get_lab_connection(config)
                    try:
                        write_memory(
                            conn,
                            selected_session_id,
                            mem_key.strip(),
                            mem_value or "",
                            provenance="poison:lab_canary",
                        )
                        st.success("Poison memory written with canary provenance.")
                    except Exception as exc:
                        st.error(f"Poison write failed: {_safe_error(exc)}")
                    finally:
                        conn.close()
                else:
                    st.warning("Memory key is required.")
        with mem_col3:
            if st.button("Quarantine All", key="mem_quarantine_btn"):
                conn = _get_lab_connection(config)
                try:
                    count = quarantine_memory(conn, namespace)
                    st.success(f"Quarantined {count} entries.")
                except Exception as exc:
                    st.error(f"Quarantine failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with mem_col4:
            if st.button("Reset Memory", key="mem_reset_btn"):
                conn = _get_lab_connection(config)
                try:
                    count = reset_memory(conn, namespace)
                    st.success(f"Reset {count} entries.")
                except Exception as exc:
                    st.error(f"Reset failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with mem_col5:
            if st.button("Export Memory", key="mem_export_btn"):
                conn = _get_lab_connection(config, readonly=True)
                try:
                    entries = list_memory_events(conn, session_id=selected_session_id)
                    if entries:
                        payload = json.dumps(entries, indent=2, default=str)
                        st.download_button(
                            "Download Memory JSON",
                            data=payload,
                            file_name=f"memory_{selected_session_id}.json",
                            mime="application/json",
                            key="mem_download",
                        )
                    else:
                        st.info("No memory entries to export.")
                finally:
                    conn.close()
    st.subheader("Poisoning Workflow")
    if selected_session_id and session:
        workflow_cols = st.columns(4)
        with workflow_cols[0]:
            if st.button("Start Poison Phase", key="phase_poison_btn"):
                conn = _get_lab_connection(config)
                try:
                    transition_session(conn, selected_session_id, "poison")
                    st.success("Session moved to poison phase.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Phase transition failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with workflow_cols[1]:
            if st.button("Start Probe Phase", key="phase_probe_btn"):
                conn = _get_lab_connection(config)
                try:
                    transition_session(conn, selected_session_id, "probe")
                    st.success("Session moved to probe phase.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Phase transition failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with workflow_cols[2]:
            if st.button("Run Detectors", key="run_detectors_btn"):
                conn = _get_lab_connection(config)
                try:
                    turns = get_turns(conn, session_id=selected_session_id)
                    assistant_turns = [
                        turn for turn in turns if turn.get("role") == "assistant"
                    ]
                    if not assistant_turns:
                        st.warning("No assistant turn is available for detection.")
                    else:
                        latest = assistant_turns[-1]
                        detections = run_detectors(conn, latest["id"], commit=True)
                        st.success(f"Recorded {len(detections)} detection signal(s).")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Detection failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with workflow_cols[3]:
            mitigation_choice = st.selectbox(
                "Mitigation",
                ["none", *sorted(STRATEGIES)],
                key="workflow_mitigation",
            )
            if st.button("Apply Mitigation", key="apply_mitigation_btn"):
                if mitigation_choice == "none":
                    st.warning("Select a mitigation strategy.")
                else:
                    conn = _get_lab_connection(config)
                    try:
                        result = apply_mitigation(
                            conn,
                            str(st.session_state.chat_experiment_id),
                            mitigation_choice,
                            session_id=selected_session_id,
                        )
                        st.success(result["result"])
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Mitigation failed: {_safe_error(exc)}")
                    finally:
                        conn.close()
        phase_row = st.columns(2)
        with phase_row[0]:
            if st.button("Start Mitigation Phase", key="phase_mitigation_btn"):
                conn = _get_lab_connection(config)
                try:
                    transition_session(conn, selected_session_id, "mitigation")
                    st.success("Session moved to mitigation phase.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Phase transition failed: {_safe_error(exc)}")
                finally:
                    conn.close()
        with phase_row[1]:
            if st.button(
                "Start Post-Mitigation Phase", key="phase_post_mitigation_btn"
            ):
                conn = _get_lab_connection(config)
                try:
                    transition_session(conn, selected_session_id, "post_mitigation")
                    st.success("Session moved to post-mitigation phase.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Phase transition failed: {_safe_error(exc)}")
                finally:
                    conn.close()

        finish_col = st.columns(1)[0]
        with finish_col:
            if st.button("Finish Experiment", key="finish_experiment_btn"):
                conn = _get_lab_connection(config)
                try:
                    finish_experiment(
                        conn,
                        str(st.session_state.chat_experiment_id),
                        status="completed",
                    )
                    st.success("Experiment finished.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Finish failed: {_safe_error(exc)}")
                finally:
                    conn.close()
    else:
        st.info("Select a session to manage memory and poisoning workflow.")


# ---------------------------------------------------------------------------
# Dashboard Page
# ---------------------------------------------------------------------------


def render_dashboard_page(
    experiment_ids: list[str] | None = None,
    experiment_summaries: dict[str, Any] | None = None,
) -> None:
    """Render the experiment dashboard page."""
    if not _streamlit_available():
        return
    import streamlit as st

    from core.experiments import get_experiment, list_experiments, list_sessions
    from core.detections import list_detections
    from core.mitigations import list_mitigations
    from core.metrics import summarize_experiment
    from core.config import load_config

    st.title("Experiment Dashboard")

    if not experiment_ids:
        st.info("No experiments yet.")
        return
    selected_exp = st.selectbox(
        "Select experiment",
        experiment_ids,
        key="dashboard_experiment",
    )
    if not selected_exp:
        st.info("No experiments available. Use the Chat Console to create one.")
        return

    config = load_config()
    conn = _get_lab_connection(config, readonly=True)
    try:
        experiment = get_experiment(conn, selected_exp)
        if experiment is None:
            st.error("Experiment not found.")
            return

        summary = (
            experiment_summaries.get(selected_exp) if experiment_summaries else None
        )
        if summary is None:
            with st.spinner("Computing summary..."):
                summary = summarize_experiment(conn, selected_exp)

        st.subheader("Summary Cards")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sessions", summary.get("sessions", 0))
        c2.metric("Turns", summary.get("turns", 0))
        c3.metric("Detections", summary.get("detections", 0))
        c4.metric("Errors", summary.get("errors", 0))
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Memory Events", summary.get("memory_events", 0))
        c6.metric("Mitigations", summary.get("mitigations", 0))
        c7.metric("Avg Latency (ms)", f"{summary.get('avg_latency_ms', 0):.1f}")
        c8.metric("Detection Rate", f"{summary.get('detection_rate', 0):.2%}")
        c9, c10, c11, c12 = st.columns(4)
        c9.metric("Active Memory", summary.get("active_memory", 0))
        c10.metric("Quarantined Memory", summary.get("quarantined_memory", 0))
        c11.metric("Suspected", summary.get("suspected_detections", 0))
        c12.metric("Status", summary.get("status", "unknown"))
        c13, c14, c15, c16 = st.columns(4)
        c13.metric("Persistence", f"{summary.get('persistence_rate', 0):.1%}")
        c14.metric("Contamination", f"{summary.get('contamination_rate', 0):.1%}")
        c15.metric("Total Cost", f"${summary.get('total_cost', 0):.8f}")
        c16.metric("Avg Cost/Turn", f"${summary.get('avg_cost_per_turn', 0):.8f}")

        st.markdown(
            f"**Model:** `{summary.get('model_id', 'N/A')}` | **Experiment:** `{selected_exp}`"
        )

        sessions = list_sessions(conn, experiment_id=selected_exp)
        st.subheader("Phase Timeline")
        if sessions:
            phase_counts: dict[str, int] = {}
            for s in sessions:
                phase = s.get("phase", "unknown")
                phase_counts[phase] = phase_counts.get(phase, 0) + 1
            phase_data = [
                {"phase": p, "sessions": c} for p, c in sorted(phase_counts.items())
            ]
            st.dataframe(phase_data, width='stretch')
            st.bar_chart({d["phase"]: d["sessions"] for d in phase_data})
        else:
            st.info("No sessions yet.")

        st.subheader("Detections")
        detections = list_detections(conn, experiment_id=selected_exp)
        if detections:
            st.dataframe(
                [
                    {
                        "id": d["id"],
                        "detector": d.get("detector"),
                        "signal": d.get("signal"),
                        "score": d.get("score"),
                        "label": d.get("label"),
                        "explanation": (d.get("explanation") or "")[:200],
                    }
                    for d in detections
                ],
                width='stretch',
                key="detections_table",
            )
        else:
            st.info("No detections recorded yet.")

        with st.expander("Detection Scores", expanded=False):
            if detections:
                scores = [d["score"] for d in detections]
                st.metric("Average Score", f"{sum(scores)/len(scores):.4f}")
                st.metric("Max Score", f"{max(scores):.4f}")
                st.metric("Min Score", f"{min(scores):.4f}")

        st.subheader("Mitigations")
        mitigations = list_mitigations(conn, experiment_id=selected_exp)
        if mitigations:
            st.dataframe(
                [
                    {
                        "id": m["id"],
                        "strategy": m.get("strategy"),
                        "applied_at": m.get("applied_at"),
                        "result": (m.get("result") or "")[:200],
                    }
                    for m in mitigations
                ],
                width='stretch',
                key="mitigations_table",
            )
            with st.expander("Memory Hash Comparison", expanded=False):
                for m in mitigations:
                    st.markdown(
                        f"**{m['strategy']}** ({m.get('applied_at')}): "
                        f"before `{str(m.get('before_memory_hash', ''))[:16]}...` → "
                        f"after `{str(m.get('after_memory_hash', ''))[:16]}...`"
                    )
        else:
            st.info("No mitigations recorded yet.")

        st.subheader("Errors & Timeouts")
        turns_with_errors = []
        for s in sessions:
            from core.experiments import get_turns

            turns = get_turns(conn, session_id=s["id"])
            for t in turns:
                if t.get("error"):
                    turns_with_errors.append(
                        {
                            "turn_id": t["id"],
                            "session_id": t["session_id"],
                            "phase": t.get("phase"),
                            "error": t["error"][:200],
                        }
                    )
        if turns_with_errors:
            st.dataframe(turns_with_errors, width='stretch')
        else:
            st.info("No errors recorded yet.")

        st.subheader("Persistence (Memory Events by Session)")
        if sessions:
            persistence_data = []
            for s in sessions:
                from core.memory import list_memory_events as _lme

                events = _lme(conn, session_id=s["id"])
                active = sum(1 for e in events if e.get("status") == "active")
                quarantined = sum(1 for e in events if e.get("status") == "quarantined")
                deleted = sum(1 for e in events if e.get("status") == "deleted")
                persistence_data.append(
                    {
                        "session_id": s["id"],
                        "phase": s.get("phase"),
                        "active": active,
                        "quarantined": quarantined,
                        "deleted": deleted,
                        "total": len(events),
                    }
                )
            st.dataframe(persistence_data, width='stretch')
        else:
            st.info("No sessions yet.")

        st.subheader("Model Comparison")
        experiments = list_experiments(conn)
        if experiments:
            model_comparison = []
            for exp in experiments:
                model_comparison.append(
                    {
                        "experiment_id": exp["id"],
                        "model_id": exp.get("model_id") or "N/A",
                        "title": exp.get("title", ""),
                        "status": exp.get("status", ""),
                        "session_count": exp.get("session_count", 0),
                        "turn_count": exp.get("turn_count", 0),
                    }
                )
            st.dataframe(model_comparison, width='stretch')
        else:
            st.info("No experiments to compare.")

        st.subheader("Drilldown: Session Details")
        if sessions:
            selected_session = st.selectbox(
                "Session",
                [s["id"] for s in sessions],
                key="drilldown_session",
            )
            if selected_session:
                drill_col1, drill_col2 = st.columns(2)
                with drill_col1:
                    st.markdown("**Turns**")
                    from core.experiments import get_turns

                    turns = get_turns(conn, session_id=selected_session)
                    if turns:
                        st.dataframe(
                            [
                                {
                                    "turn_id": t["id"],
                                    "sequence": t.get("sequence"),
                                    "role": t.get("role"),
                                    "phase": t.get("phase"),
                                    "content": (t.get("content") or "")[:100],
                                    "error": t.get("error") or "",
                                }
                                for t in turns
                            ],
                            width='stretch',
                        )
                with drill_col2:
                    st.markdown("**Memory Events**")
                    from core.memory import list_memory_events as _lme2

                    events = _lme2(conn, session_id=selected_session)
                    if events:
                        st.dataframe(
                            [
                                {
                                    "id": e["id"][:12],
                                    "operation": e.get("operation"),
                                    "key": e.get("memory_key"),
                                    "provenance": e.get("provenance", ""),
                                    "status": e.get("status"),
                                }
                                for e in events
                            ],
                            width='stretch',
                        )
    finally:
        conn.close()
