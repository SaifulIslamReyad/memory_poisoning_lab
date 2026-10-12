"""Agentic AI Security Lab — Streamlit application entry point."""

from __future__ import annotations

# Import sys for path manipulation and stderr output
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


def ensure_databases() -> tuple[str, str]:
    """Initialize models and lab databases; import snapshot when models empty."""
    # Import catalog functions for listing models and importing snapshots
    from core.catalog import import_model_snapshot, list_models
    # Import the configuration loader
    from core.config import load_config
    # Import database initialization helpers
    from core.database import initialize_lab_db, initialize_models_db

    # Load the application configuration (paths, settings, etc.)
    config = load_config()
    # Get paths to the models and lab databases from config
    models_db = config.models_db
    lab_db = config.lab_db

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

    # Import archive listing function for the Archive page
    from core.archive import list_archives
    # Import catalog functions for model listing and stats
    from core.catalog import catalog_stats, list_models
    # Import the configuration loader
    from core.config import load_config
    # Import the SQLite connection helper
    from core.database import connect_sqlite
    # Import experiment listing function
    from core.experiments import list_experiments

    # Configure the Streamlit page layout (title and wide layout)
    st.set_page_config(page_title="Agentic AI Security Lab", layout="wide")

    # Ensure both databases are initialized and get their paths
    models_db, lab_db = ensure_databases()

    # Render the sidebar navigation with the app title and page options
    st.sidebar.title("Agentic AI Security Lab")
    # The radio buttons determine which page to show; default is the first option
    page = st.sidebar.radio(
        "Navigate",
        ["Catalog", "Chat Console", "Dashboard", "Archive"],
    )

    # Load configuration (provides API keys, paths, and settings)
    config = load_config()
    # Fetch all models from the catalog (including inactive ones)
    models = list_models(models_db, active_only=False)
    # Compute catalog statistics, or use zeroed defaults if no models exist
    stats = catalog_stats(models_db) if models else {"total": 0, "active": 0, "features": 0, "parameters": 0}

    # Route to the appropriate page renderer based on the selected sidebar option
    if page == "Catalog":
        # Render the model catalog page with all models and computed stats
        from gui.pages import render_catalog_page

        render_catalog_page(models=models, stats=stats)

    elif page == "Chat Console":
        # Render the chat console page
        from gui.pages import render_chat_page

        # Open a read-only connection to the lab database
        lab_connection = connect_sqlite(lab_db, readonly=True)
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
        lab_connection = connect_sqlite(lab_db, readonly=True)
        try:
            # Fetch all experiment IDs
            experiment_ids = [exp["id"] for exp in list_experiments(lab_connection)]
            # Build a dict of experiment summaries for display
            experiment_summaries = {}
            for exp_id in experiment_ids:
                from core.metrics import summarize_experiment

                try:
                    # Compute summary metrics for each experiment
                    experiment_summaries[exp_id] = summarize_experiment(lab_connection, exp_id)
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

    elif page == "Archive":
        # Render the archive management page
        from gui.pages import render_archive_page

        # Fetch all archived experiments from the lab database
        archives = list_archives(config)
        # Render the archive page with the list of archives
        render_archive_page(archives=archives)


if __name__ == "__main__":
    # When run directly (python app.py), start the Streamlit application
    main()
