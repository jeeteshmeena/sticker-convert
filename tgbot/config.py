"""Bot configuration, loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))
    allowed_user_ids: set[int] = field(default_factory=lambda: _ids("ALLOWED_USER_IDS"))
    work_dir: Path = field(
        default_factory=lambda: Path(os.getenv("WORK_DIR", "./bot_data")).resolve()
    )
    max_concurrent_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_JOBS", 2))
    processes_per_job: int = field(default_factory=lambda: _int("PROCESSES_PER_JOB", 2))
    job_timeout: int = field(default_factory=lambda: _int("JOB_TIMEOUT_SECONDS", 1800))
    max_files: int = field(default_factory=lambda: _int("MAX_FILES_PER_JOB", 120))
    default_author: str = field(default_factory=lambda: os.getenv("DEFAULT_AUTHOR", "").strip())
    default_emoji: str = field(default_factory=lambda: os.getenv("DEFAULT_EMOJI", "😀").strip() or "😀")
    signal_uuid: str = field(default_factory=lambda: os.getenv("SIGNAL_UUID", "").strip())
    signal_password: str = field(default_factory=lambda: os.getenv("SIGNAL_PASSWORD", "").strip())
    log_channel_id: str = field(default_factory=lambda: os.getenv("LOG_CHANNEL_ID", "").strip())

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise SystemExit("BOT_TOKEN missing. Copy .env.example to .env and fill it in.")
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @property
    def signal_upload_enabled(self) -> bool:
        return bool(self.signal_uuid and self.signal_password)

    def is_allowed(self, user_id: int) -> bool:
        if user_id in self.admin_ids:
            return True
        return not self.allowed_user_ids or user_id in self.allowed_user_ids


CONFIG = Config()
