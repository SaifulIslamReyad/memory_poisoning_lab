"""Core services for Agentic AI Security Lab.

This package exposes submodules that provide the security lab's core
infrastructure: configuration management, PostgreSQL persistence, model
cataloging, agent execution, sandboxing, memory management, safety
detectors, mitigations, metrics, and background job processing.
"""

__all__ = [
    "agent",        # AgentRunner: executes model requests with security controls
    "catalog",      # Model catalog: persists and syncs model metadata from Groq
    "config",       # LabConfig: environment-based configuration and redaction
    "database",     # DB utilities: schema constants, connection, and migration helpers
    "detections",   # Canary and provenance detection signals
    "experiments",  # Experiment lifecycle: CRUD for sessions, turns, and experiments
    "memory",       # Secure memory: write/query/quarantine/delete with provenance policies
    "metrics",      # Metric recording and experiment-level summary aggregation
    "mitigations",  # Mitigation strategies with before/after memory hash evidence
    "models",       # Model capability normalization and request validation
    "tools",        # Read-only SQL sandbox and tool authorization
    "workers",      # Job manager and async worker process management
]
