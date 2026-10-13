"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # --- secrets -----------------------------------------------------------
    # One root secret; tokens for different audiences are separated by `aud`.
    secret: str = field(default_factory=lambda: os.environ.get("PLATFORM_SECRET", "dev-secret-change-me-0123456789abcdef"))
    demo_login: bool = field(default_factory=lambda: os.environ.get("DEMO_LOGIN", "1") == "1")

    # --- agent runtime -----------------------------------------------------
    max_iterations: int = field(default_factory=lambda: _int("AGENT_MAX_ITERATIONS", 10))
    checkpoint_db: str = field(default_factory=lambda: os.environ.get("CHECKPOINT_DB", "data/checkpoints.sqlite"))
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "mock"))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", ""))
    mock_token_delay_ms: int = field(default_factory=lambda: _int("MOCK_TOKEN_DELAY_MS", 15))

    # --- MCP ---------------------------------------------------------------
    # Empty => the admin MCP server runs in-process. Set to a streamable-HTTP
    # URL (e.g. http://mcp:8001/mcp) to talk to a separately deployed server.
    mcp_url: str = field(default_factory=lambda: os.environ.get("MCP_URL", ""))

    # --- SpiceDB -----------------------------------------------------------
    # Empty => in-memory evaluator of the same schema (dev / unit tests only).
    spicedb_endpoint: str = field(default_factory=lambda: os.environ.get("SPICEDB_ENDPOINT", ""))
    spicedb_token: str = field(default_factory=lambda: os.environ.get("SPICEDB_TOKEN", "dev-spicedb-key"))
    spicedb_insecure: bool = field(default_factory=lambda: os.environ.get("SPICEDB_INSECURE", "1") == "1")

    # --- gateway -----------------------------------------------------------
    replay_buffer: int = field(default_factory=lambda: _int("WS_REPLAY_BUFFER", 4096))
    send_queue: int = field(default_factory=lambda: _int("WS_SEND_QUEUE", 8192))
    cors_origins: str = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", "http://localhost:5173"))


def get_settings() -> Settings:
    return Settings()
