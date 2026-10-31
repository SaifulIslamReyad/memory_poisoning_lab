"""Agentic AI Security Lab — Streamlit application entry point."""

from __future__ import annotations

# Import standard-library helpers for access control and path handling
import hmac
import os
import sys

# Import Path for filesystem path operations
from pathlib import Path

# Resolve the project root directory (parent of this file) and add it to sys.path
# so that 'core' and 'gui' packages can be imported without installation
_PROJECT_ROOT = Path(__file__).resolve().parents[0]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _streamlit_available() -> bool:
    """Check whether the Streamlit library is installed and importable."""
    try:
        # Attempt to import streamlit (the # noqa: F401 suppresses unused-import linter warnings)
        import streamlit  # noqa: F401

        return True
    except ImportError:
        # Streamlit is not installed
        return False


def _require_access(st: object) -> None:
    """Require APP_PASSWORD when the app is exposed beyond a trusted machine."""
    password = os.getenv("APP_PASSWORD")
    if not password:
        return
    if st.session_state.get("authenticated"):
        return
    st.title("Agentic AI Security Lab")
    entered = st.text_input("Access password", type="password")
    if st.button("Unlock"):
        if hmac.compare_digest(entered, password):
            st.session_state.authenticated = True
            st.rerun()
        st.error("Invalid access password.")
    st.stop()


def ensure_databases() -> tuple[str, str]:
    """Initialize hosted PostgreSQL schemas and seed the model catalog."""
    # Import catalog functions for listing models and importing snapshots
    from core.catalog import import_model_snapshot, list_models

    # Import the configuration loader
    from core.config import load_config

    # Import database initialization helpers
    from core.database import initialize_lab_db, initialize_models_db

    # Load the application configuration (paths, settings, etc.)
    config = load_config()
    if not config.database_url:
        raise RuntimeError(
            "DATABASE_URL is required. Configure the hosted PostgreSQL/Supabase "
            "connection string before starting the application."
        )
    models_db = config.database_url
    lab_db = config.database_url

    # Initialize (create schema for) the models database; returns a live connection
    models_connection = initialize_models_db(models_db)
    try:
        # Initialize (create schema for) the lab database; returns a live connection
        lab_connection = initialize_lab_db(lab_db)
        # Close the lab connection immediately since we only needed the schema
        lab_connection.close()
        # If the models database already has models, no import is needed
        if list_models(models_db, active_only=False):
            return str(models_db), str(lab_db)
        # Otherwise, import model definitions from the snapshot JSON file
        import_model_snapshot(config.model_snapshot, db_path=models_db)
    finally:
        # Always close the models connection when done
        models_connection.close()
    return str(models_db), str(lab_db)


def main() -> None:
    """Entry point: sets up Streamlit, initializes databases, and renders the selected page."""
    # If Streamlit isn't installed, print an error and exit
    if not _streamlit_available():
        print(
            "Streamlit is required to run the GUI. Install it with:\n"
            "  pip install streamlit",
            file=sys.stderr,
        )
        sys.exit(1)

    # Import Streamlit now that we know it's available
    import streamlit as st

    # Import catalog functions for model listing and stats
    from core.catalog import catalog_stats, list_models

    # Import the configuration loader
    from core.config import load_config

    # Import the SQLite connection helper
    from core.database import connect_database

    # Import experiment listing function
    from core.experiments import list_experiments

    # Configure the Streamlit page layout (title and wide layout)
    st.set_page_config(page_title="Agentic AI Security Lab", layout="wide")
    _require_access(st)

    # Ensure the hosted database is initialized and get its connection target
    models_db, lab_db = ensure_databases()

    # Render the sidebar navigation with the app title and page options
    st.sidebar.title("Agentic AI Security Lab")
    # The radio buttons determine which page to show; default is the first option
    page = st.sidebar.radio(
        "Navigate",
        ["Catalog", "Chat Console", "Dashboard"],
    )

    # Load configuration (provides API keys, paths, and settings)
    config = load_config()
    # Fetch all models from the catalog (including inactive ones)
    models = list_models(models_db, active_only=False)
    # Compute catalog statistics, or use zeroed defaults if no models exist
    stats = (
        catalog_stats(models_db)
        if models
        else {"total": 0, "active": 0, "features": 0, "parameters": 0}
    )

    # Route to the appropriate page renderer based on the selected sidebar option
    if page == "Catalog":
        # Render the model catalog page with all models and computed stats
        from gui.pages import render_catalog_page

        render_catalog_page(models=models, stats=stats)

    elif page == "Chat Console":
        # Render the chat console page
        from gui.pages import render_chat_page

        # Open a read-only connection to the lab database
        lab_connection = connect_database(lab_db, readonly=True)
        try:
            # Fetch all experiment IDs to populate the chat page's experiment selector
            experiment_ids = [exp["id"] for exp in list_experiments(lab_connection)]
        finally:
            # Always close the read-only connection
            lab_connection.close()
        # Render the chat page, passing available models and experiment IDs
        render_chat_page(models=models, experiment_ids=experiment_ids)

    elif page == "Dashboard":
        # Render the experiment dashboard page
        from gui.pages import render_dashboard_page

        # Open a read-only connection to the lab database
        lab_connection = connect_database(lab_db, readonly=True)
        try:
            # Fetch all experiment IDs
            experiment_ids = [exp["id"] for exp in list_experiments(lab_connection)]
            # Build a dict of experiment summaries for display
            experiment_summaries = {}
            for exp_id in experiment_ids:
                from core.metrics import summarize_experiment

                try:
                    # Compute summary metrics for each experiment
                    experiment_summaries[exp_id] = summarize_experiment(
                        lab_connection, exp_id
                    )
                except KeyError:
                    # Skip experiments that can't be summarized (e.g., missing data)
                    pass
        finally:
            # Always close the read-only connection
            lab_connection.close()
        # Render the dashboard with experiment IDs and their summaries
        render_dashboard_page(
            experiment_ids=experiment_ids,
            experiment_summaries=experiment_summaries,
        )


if __name__ == "__main__":
    # When run directly (python app.py), start the Streamlit application
    main()
