"""Application configuration and credential handling."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# Resolve the project root directory (two levels up from this file: core/ -> project root)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp string with 'Z' suffix."""
    # Get current UTC time, convert to ISO format, replace +00:00 with Z for RFC 3339 compliance
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str = "") -> str:
    """Generate a random UUID hex string, optionally prefixed (e.g., 'exp_', 'ses_')."""
    value = uuid4().hex
    return f"{prefix}{value}" if prefix else value


def json_dumps(value: Any) -> str:
    """Serialize a value to a compact, deterministic JSON string."""
    # sort_keys=True ensures consistent key ordering for reproducible hashes
    # default=str handles non-serializable types by converting to string
    # separators=(",", ":") produces compact output without unnecessary spaces
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def canonical_json(value: Any) -> bytes:
    """Serialize a value to canonical JSON bytes (stable representation for hashing)."""
    return json_dumps(value).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Compute the SHA-256 hex digest of a byte string."""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file, reading in 1 MB chunks."""
    digest = hashlib.sha256()
    # Open in binary mode and read in chunks to handle large files efficiently
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_text(value: str) -> str:
    """Redact credential-like patterns from a text string in-place."""
    stripped = value.strip()
    # If the string is empty or whitespace-only, return it unchanged
    if not stripped:
        return value
    # Pattern 1: "Bearer <token>" — OAuth/Bearer authorization headers
    bearer_pattern = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
    if bearer_pattern.search(stripped):
        value = bearer_pattern.sub("Bearer [REDACTED]", value)
    # Pattern 2: key=value or key: value assignments where key looks like a credential name
    # Matches: api_key, API-Key, authorization, auth, secret, secret_key, client_secret,
    #          password, passwd, token, access_token, credentials
    # Requires the value to be at least 8 chars of credential-like characters
    assignment_pattern = re.compile(
        r"(?i)\b(api[_-]?key|authorization|auth|secret(?:_key)?|client[_-]?secret|"
        r"password|passwd|token|access[_-]?token|credential(?:s)?)\b"
        r"(\s*[:=]\s*)([\"']?)([A-Za-z0-9._~+/=-]{8,})([\"']?)"
    )
    value = assignment_pattern.sub(
        # Replace the credential value (group 4) with [REDACTED], preserving surrounding text
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED]{match.group(5)}",
        value,
    )
    # Pattern 3: Bare API key tokens like "sk_..." or "gsk_..." (OpenAI/Groq format)
    secret_pattern = re.compile(r"\b(?:sk|gsk)[_-][A-Za-z0-9._~+/=-]{16,}\b")
    return secret_pattern.sub("[REDACTED]", value)


def _is_sensitive_key(key: str) -> bool:
    """Determine whether a dictionary key name suggests a sensitive credential value."""
    # Normalize the key: replace hyphens with underscores
    raw = str(key).replace("-", "_")
    # Convert camelCase/snake_case mix to snake_case for comparison
    # e.g., "apiKey" -> "api_Key" -> "api_key"
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).lower()
    # Set of exact key names that are always considered sensitive
    exact = {
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "secret",
        "secret_key",
        "client_secret",
        "password",
        "passwd",
        "token",
        "access_token",
        "credential",
        "credentials",
        "bearer",
    }
    return (
        # Exact match in the sensitive set
        normalized in exact
        # Suffix-based matching: any key ending in _api_key or _token
        or normalized.endswith("_api_key")
        or normalized.endswith("_token")
        # Nested key access like "request.headers.authorization"
        or normalized.endswith(".authorization")
    )


def redact(value: Any) -> Any:
    """Recursively redact credential-like values in any nested structure without mutating the input."""
    # For dicts: check each key; if sensitive, replace value with [REDACTED], else recurse
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item)
        return result
    # For lists/tuples/sets: recurse into each element
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    # For strings: if it looks like JSON, parse and recursively redact the parsed object
    if isinstance(value, str):
        stripped = value.strip()
        # Only attempt JSON parsing if the string looks like a JSON object or array
        if stripped.startswith(("{", "[")) and len(stripped) > 2:
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                parsed = None
            # If parsing succeeded and redaction changed something, return redacted JSON string
            if parsed is not None:
                redacted = redact(parsed)
                if redacted != parsed:
                    return json_dumps(redacted)
        # Apply string-level redaction (bearer, assignment, bare key patterns)
        return _redact_text(value)
    # For any other type (int, float, bool, None): return as-is
    return value


@dataclass(frozen=True)
class LabConfig:
    """Immutable configuration for the lab application."""

    # Root directory of the project (used for default paths)
    base_dir: Path = _PROJECT_ROOT
    # Path to the models catalog SQLite database
    models_db: Path = _PROJECT_ROOT / "data" / "models.sqlite"
    # Path to the lab experiment SQLite database (stores all experiment evidence)
    lab_db: Path = _PROJECT_ROOT / "data" / "lab.sqlite"
    # Directory for exported experiment archives
    archives_dir: Path = _PROJECT_ROOT / "data" / "archives"
    # Directory for per-experiment sandbox databases (copy of fixture, read-only)
    sandbox_dir: Path = _PROJECT_ROOT / "data" / "sandbox"
    # Path to the e-commerce fixture database (source of truth, never modified by runtime)
    fixture_db: Path = _PROJECT_ROOT / "data" / "ecommerce.db"
    # Path to the bundled model snapshot JSON file (imported when models DB is empty)
    model_snapshot: Path = _PROJECT_ROOT / "all_models_in_grok.json"
    # Optional Neon/PostgreSQL connection URL. When set, it replaces both SQLite stores.
    database_url: str | None = None
    # Base URL for the Groq-compatible API
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Timeout for API requests (seconds)
    request_timeout: float = 30.0
    # Maximum rows returned by read-only SQL tool
    max_rows: int = 100
    # Maximum total byte size of SQL tool results
    max_result_bytes: int = 1024 * 1024
    # Maximum SQLite VM steps for SQL tool queries (prevents runaway queries)
    max_query_steps: int = 100000

    @property
    def api_key(self) -> str | None:
        """Get the Groq API key from environment, or None if not set."""
        return os.getenv("GROQ_API_KEY")

    def require_api_key(self) -> str:
        """Get the API key or raise an explanatory error if it's missing."""
        key = self.api_key
        if not key:
            raise ValueError(
                "GROQ_API_KEY is required. Set it in the environment; it is never stored by the lab."
            )
        return key

    def ensure_directories(self) -> None:
        """Create all required directories if they don't already exist."""
        for path in (
            self.models_db.parent,
            self.lab_db.parent,
            self.archives_dir,
            self.sandbox_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def database_target(self) -> str | Path:
        """Return the configured shared database target for repositories."""
        return self.database_url or self.lab_db

    @property
    def models_target(self) -> str | Path:
        """Return the configured model-catalog database target."""
        return self.database_url or self.models_db


def load_config(base_dir: str | Path | None = None) -> LabConfig:
    """Load configuration from environment variables and optional .env file."""
    # Attempt to load variables from a .env file if python-dotenv is installed
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # Resolve the root directory: explicit base_dir if provided, otherwise project root
    root = Path(base_dir).expanduser().resolve() if base_dir else _PROJECT_ROOT
    data_dir = root / "data"
    # Build the config, overriding defaults from environment variables where set
    config = LabConfig(
        base_dir=root,
        models_db=data_dir / "models.sqlite",
        lab_db=data_dir / "lab.sqlite",
        archives_dir=data_dir / "archives",
        sandbox_dir=data_dir / "sandbox",
        fixture_db=data_dir / "ecommerce.db",
        model_snapshot=root / "all_models_in_grok.json",
        database_url=os.getenv("DATABASE_URL") or None,
        # Allow overriding the Groq API URL via environment variable
        groq_base_url=os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1",
        # Allow overriding request timeout via environment variable (default 30s)
        request_timeout=float(os.getenv("GROQ_REQUEST_TIMEOUT", "30")),
        # Allow overriding max rows via environment variable (default 100)
        max_rows=int(os.getenv("LAB_MAX_ROWS", "100")),
        # Allow overriding max result bytes via environment variable (default 1 MB)
        max_result_bytes=int(os.getenv("LAB_MAX_RESULT_BYTES", str(1024 * 1024))),
        # Allow overriding max query steps via environment variable (default 100,000)
        max_query_steps=int(os.getenv("LAB_MAX_QUERY_STEPS", "100000")),
    )
    # Ensure all required directories exist before returning
    config.ensure_directories()
    return config
