"""Runs sticker-convert's CLI as a subprocess for one bot job."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .config import CONFIG

REPO_SRC = Path(__file__).resolve().parent.parent / "src"

# Output targets the bot offers. key -> (button label, preset, export flag or None)
TARGETS: dict[str, tuple[str, str, str | None]] = {
    "whatsapp": ("🟢 WhatsApp (.wastickers)", "whatsapp", "--export-whatsapp"),
    "telegram": ("🔵 Telegram sticker pack", "telegram", "--export-telegram"),
    "telegram_emoji": ("😀 Telegram emoji pack", "telegram_emoji", "--export-telegram-emoji"),
    "signal": ("🔷 Signal", "signal", None),  # becomes --export-signal when creds exist
    "discord": ("🎮 Discord files", "discord", None),
    "imessage": ("🍏 iMessage (Xcode project)", "imessage_medium", "--export-imessage"),
    "raw": ("📦 Original files only (no convert)", "auto", None),
}

# Link patterns sticker-convert's --download-auto understands
LINK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Telegram", re.compile(r"(https?://)?(t\.me|telegram\.me)/(addstickers|addemoji)/\w+", re.I)),
    ("Signal", re.compile(r"https?://signal\.art/addstickers/#pack_id=\w+&pack_key=\w+", re.I)),
    ("LINE", re.compile(r"(https?://(store\.)?line\.me/\S+|line://shop/detail/\d+)", re.I)),
    ("Kakao", re.compile(r"https?://(e\.kakao\.com/t/|emoticon\.kakao\.com/items/)\S+", re.I)),
    ("Naver Band", re.compile(r"https?://(www\.)?band\.us/sticker/\S+", re.I)),
    ("OGQ", re.compile(r"https?://ogqmarket\.naver\.com/\S+", re.I)),
    ("Viber", re.compile(r"https?://stickers\.viber\.com/pages/\S+", re.I)),
]


def detect_link(text: str) -> tuple[str, str] | None:
    for name, pat in LINK_PATTERNS:
        m = pat.search(text or "")
        if m:
            url = m.group(0)
            if name == "Telegram" and not url.lower().startswith("http"):
                url = "https://" + url
            return name, url
    return None


@dataclass
class Job:
    user_id: int
    target: str
    title: str | None = None
    author: str | None = None
    url: str | None = None  # download source; None means local files
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False

    @property
    def root(self) -> Path:
        return CONFIG.work_dir / "jobs" / f"{self.user_id}_{self.job_id}"

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def prepare(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class JobResult:
    ok: bool
    urls: list[str]
    files: list[Path]
    log_tail: str
    summary: str


def build_command(job: Job) -> list[str]:
    _, preset, export_flag = TARGETS[job.target]
    cmd = [sys.executable, "-m", "sticker_convert", "--no-confirm", "--no-progress"]

    if job.url:
        cmd += ["--download-auto", job.url]
    cmd += ["--input-dir", str(job.input_dir), "--output-dir", str(job.output_dir)]

    if job.target == "raw":
        cmd += ["--no-compress"]
    else:
        cmd += ["--preset", preset, "--processes", str(CONFIG.processes_per_job)]

    if job.target == "signal" and CONFIG.signal_upload_enabled:
        export_flag = "--export-signal"
        cmd += ["--signal-uuid", CONFIG.signal_uuid, "--signal-password", CONFIG.signal_password]
    if export_flag:
        cmd.append(export_flag)

    if job.title:
        cmd += ["--title", job.title]
    if job.author:
        cmd += ["--author", job.author]
    cmd += ["--default-emoji", CONFIG.default_emoji]

    # Telegram token is needed to download Telegram packs and to upload to Telegram.
    cmd += ["--telegram-token", CONFIG.bot_token, "--telegram-userid", str(job.user_id)]
    return cmd


def _redact(line: str) -> str:
    line = line.replace(CONFIG.bot_token, "<token>")
    if CONFIG.signal_password:
        line = line.replace(CONFIG.signal_password, "<hidden>")
    return line


async def run_job(
    job: Job,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> JobResult:
    job.prepare()
    env = os.environ.copy()
    if REPO_SRC.exists():
        env["PYTHONPATH"] = str(REPO_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"

    job.proc = await asyncio.create_subprocess_exec(
        *build_command(job),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(job.root),
        env=env,
    )

    lines: list[str] = []
    last_push = 0.0

    async def reader() -> None:
        nonlocal last_push
        assert job.proc and job.proc.stdout
        async for raw in job.proc.stdout:
            line = _redact(raw.decode("utf-8", "replace").rstrip())
            if not line.strip() or set(line.strip()) <= {"#"}:
                continue
            lines.append(line)
            if len(lines) > 400:
                del lines[:200]
            now = time.monotonic()
            if on_progress and now - last_push > 4:
                last_push = now
                try:
                    await on_progress(line)
                except Exception:
                    pass

    try:
        await asyncio.wait_for(reader(), timeout=CONFIG.job_timeout)
        await job.proc.wait()
    except asyncio.TimeoutError:
        job.proc.kill()
        lines.append(f"Timed out after {CONFIG.job_timeout}s")

    text = "\n".join(lines)
    summary = "\n".join(
        l for l in lines if l.startswith(("Download:", "Compress:", "Export:"))
    )

    urls: list[str] = []
    res = job.output_dir / "export-result.txt"
    if res.exists():
        for u in res.read_text(encoding="utf-8").splitlines():
            u = u.strip()
            if u.startswith("http"):
                urls.append(u)

    files = collect_outputs(job)
    ok = (job.proc.returncode == 0) and not job.cancelled and bool(urls or files)
    return JobResult(ok=ok, urls=urls, files=files, log_tail=text[-1500:], summary=summary)


def collect_outputs(job: Job) -> list[Path]:
    out = job.output_dir
    if not out.exists():
        return []
    packs = sorted(out.glob("*.wastickers"))
    if job.target == "whatsapp" and packs:
        return packs

    if job.target == "imessage":
        projects = [p for p in out.iterdir() if p.is_dir()]
        if projects:
            return [zip_paths(projects, job.root / f"imessage_{job.job_id}.zip")]

    if job.target in ("telegram", "telegram_emoji"):
        return []  # result is the pack link

    media = [
        p for p in sorted(out.iterdir())
        if p.is_file() and p.name not in ("export-result.txt",)
    ]
    if not media:
        return []
    name = re.sub(r"[^\w-]+", "_", job.title or job.target)[:40] or "stickers"
    return [zip_paths(media, job.root / f"{name}_{job.target}.zip")]


def zip_paths(paths: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(p.parent))
            else:
                z.write(p, p.name)
    return dest
