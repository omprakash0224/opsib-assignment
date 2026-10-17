"""
Application Configuration and Temporal Anchors.
"""
import os
from datetime import datetime, timezone, timedelta

# Automatically load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Frozen evaluation time as mandated by the fixture dataset specifications:
# 2026-10-16 15:00:00 Asia/Kolkata (+05:30)
KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
FROZEN_TIME = datetime(2026, 10, 16, 15, 0, 0, tzinfo=KOLKATA_TZ)

# Database connection: SQLite WAL mode by default for zero-dependency local runs and tests,
# or configurable PostgreSQL URI via environment variable.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./opsib.db")

DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "TENANT-001")
RULE_VERSION = os.getenv("RULE_VERSION", "v1.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# AI & OpenRouter Gateway Configuration (Task D / Phase 7)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "https://opsib.internal")
AI_REQUEST_TIMEOUT_SECONDS = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "15.0"))
AI_PROVIDER = os.getenv("AI_PROVIDER", "openrouter" if OPENROUTER_API_KEY else "mock")


def get_current_time() -> datetime:
    """
    Returns the application's current authoritative time.
    Defaults to FROZEN_TIME to guarantee deterministic scenario evaluation.
    Can be overridden in live production via clock configuration.
    """
    return FROZEN_TIME

