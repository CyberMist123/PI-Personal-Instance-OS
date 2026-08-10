from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact import strip_html
from .config import _bounded_int
from .mastodon_client import MastodonApiError
from .server import Runtime
from .transcribe import (
    DEFAULT_HOTWORDS,
    SIMPLIFIED_PROMPT,
    model_dir_ready,
    transcribe_file,
)

TRANSCRIPT_PREFIX = "🎙️ 语音转写：\n"
BATCH_LIMIT = 30
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    model_dir: str
    device: str = "cpu"
    compute_type: str = "int8"
    language: str = "zh"
    initial_prompt: str = SIMPLIFIED_PROMPT
    hotwords: str = DEFAULT_HOTWORDS
    beam_size: int = 5
    poll_seconds: int = 120
    max_audio_seconds: int = 1800
    max_audio_bytes: int = 200 * 1024 * 1024

    @classmethod
    def load(cls) -> "WorkerConfig":
        language = os.getenv("CMX_WHISPER_LANGUAGE", "zh").strip()
        if language.lower() == "auto":
            language = ""
        return cls(
            model_dir=os.getenv("CMX_WHISPER_MODEL_DIR", "").strip(),
            device=os.getenv("CMX_WHISPER_DEVICE", "cpu").strip() or "cpu",
            compute_type=os.getenv("CMX_WHISPER_COMPUTE", "int8").strip() or "int8",
            language=language,
            initial_prompt=_bounded_text(
                "CMX_WHISPER_INITIAL_PROMPT", SIMPLIFIED_PROMPT, max_chars=1000
            ),
            hotwords=_bounded_text("CMX_WHISPER_HOTWORDS", DEFAULT_HOTWORDS, max_chars=1000),
            beam_size=_bounded_int("CMX_WHISPER_BEAM_SIZE", 5, 1, 10),
            poll_seconds=_bounded_int("CMX_WORKER_POLL_SECONDS", 120, 30, 3600),
            max_audio_seconds=_bounded_int("CMX_WHISPER_MAX_SECONDS", 1800, 30, 7200),
            max_audio_bytes=_bounded_int(
                "CMX_WORKER_MAX_AUDIO_BYTES", 200 * 1024 * 1024, 1024 * 1024, 1024**3
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CMX worker daemon (voice transcription) for one resident"
    )
    parser.add_argument("--bot", required=True, help="Bot ID stored in local SQLite")
    parser.add_argument("--once", action="store_true", help="Run a single poll pass and exit")
    parser.add_argument(
        "--poll-seconds", type=int, default=None, help="Override CMX_WORKER_POLL_SECONDS"
    )
    args = parser.parse_args()

    config = WorkerConfig.load()
    if not model_dir_ready(config.model_dir):
        # Refuse at startup rather than per-item: a directory that exists but
        # holds no model.bin used to start fine and then fail on every status.
        print(
            "CMX_WHISPER_MODEL_DIR must point at a local faster-whisper model "
            f"directory containing model.bin (got: {config.model_dir or '<unset>'})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    poll_seconds = config.poll_seconds if args.poll_seconds is None else args.poll_seconds
    if not 30 <= poll_seconds <= 3600:
        print("--poll-seconds must be between 30 and 3600", file=sys.stderr)
        raise SystemExit(2)

    runtime = Runtime(args.bot)
    try:
        while True:
            try:
                run_once(runtime, config)
            except MastodonApiError as exc:
                _log(f"poll failed: {exc}")
            if args.once:
                break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        _log("worker stopped by SIGINT")
    finally:
        runtime.close()


def run_once(runtime: Any, config: WorkerConfig) -> dict[str, int]:
    """One poll pass: transcribe every new audio status and advance the watermark."""
    bot_id = runtime.bot.bot_id
    key = f"worker_watermark_{bot_id}"
    watermark = runtime.db.get_setting(key)
    if watermark is None:
        # First run: take the newest page and process it oldest -> newest.
        raw_items = runtime.client.home_timeline(limit=BATCH_LIMIT).items[:BATCH_LIMIT]
        ordered = list(reversed(raw_items))
    else:
        # Mastodon min_id returns the page immediately newer than this ID.
        raw_items = runtime.client.home_timeline(limit=BATCH_LIMIT, min_id=watermark).items[:BATCH_LIMIT]
        ordered = sorted(raw_items, key=_status_sort_key)

    own_account_id = _own_account_id(runtime)
    new_watermark = watermark
    seen = 0
    transcribed = 0
    for raw in ordered:
        outer_id = str(raw.get("id") or "")
        if outer_id and _is_newer(outer_id, new_watermark):
            new_watermark = outer_id
        source = raw.get("reblog") or raw
        source_id = str(source.get("id") or "")
        if not source_id:
            continue
        seen += 1
        if runtime.db.worker_is_done(bot_id, source_id):
            continue
        if _process_status(runtime, config, source, source_id, own_account_id):
            transcribed += 1

    if new_watermark and new_watermark != watermark:
        runtime.db.set_setting(key, new_watermark)
    return {"seen": seen, "transcribed": transcribed}


def _process_status(
    runtime: Any,
    config: WorkerConfig,
    source: dict[str, Any],
    source_id: str,
    own_account_id: str,
) -> bool:
    bot_id = runtime.bot.bot_id
    attachments = [
        item
        for item in (source.get("media_attachments") or [])
        if str(item.get("type") or "") == "audio"
    ]
    if not attachments:
        runtime.db.worker_mark_done(bot_id, source_id)
        return False

    if strip_html(source.get("content")).strip():
        # The post already carries its own transcript: since voice widget v2 the
        # web recorder transcribes before publishing, so the reply is only a
        # fallback for voice statuses that arrived with an empty body.
        _log(f"skip status {source_id}: text already present")
        runtime.db.worker_mark_done(bot_id, source_id)
        return False

    author_id = str((source.get("account") or {}).get("id") or "")
    if own_account_id and author_id == own_account_id:
        # The worker replies with its own resident token; never transcribe its
        # own bubbles or the daemon would answer itself forever.
        _log(f"skip own status {source_id}")
        runtime.db.worker_mark_done(bot_id, source_id)
        return False

    url = str(attachments[0].get("url") or attachments[0].get("remote_url") or "")
    if not url:
        _log(f"status {source_id} has an audio attachment without a URL")
        runtime.db.worker_mark_done(bot_id, source_id)
        return False

    temp_dir = Path(runtime.paths.runtime) / "worker-tmp"
    temp_path = temp_dir / f"{bot_id}-{source_id}{_suffix(url)}"
    published = False
    try:
        try:
            runtime.client.download_file(url, temp_path, max_bytes=config.max_audio_bytes)
        except MastodonApiError as exc:
            _log(f"status {source_id} audio download failed: {exc}")
            runtime.db.worker_mark_done(bot_id, source_id)
            return False

        result = transcribe_file(
            temp_path,
            model_dir=config.model_dir,
            device=config.device,
            compute_type=config.compute_type,
            language=config.language,
            initial_prompt=config.initial_prompt,
            hotwords=config.hotwords,
            beam_size=config.beam_size,
            max_audio_seconds=float(config.max_audio_seconds),
        )
        _log(f"status {source_id} ASR engine={result.get('engine', 'unknown')}")
        if result.get("error"):
            _log(f"status {source_id} transcription error: {result['error']} {result.get('detail', '')}".strip())
            runtime.db.worker_mark_done(bot_id, source_id)
            _audit(runtime, source_id, ok=False, detail=str(result["error"]))
            return False

        transcript = str(result.get("text") or "").strip()
        if not transcript:
            _log(f"status {source_id} produced an empty transcript")
            runtime.db.worker_mark_done(bot_id, source_id)
            return False

        text = _reply_text(transcript, getattr(runtime.settings, "max_status_chars", 5000))
        visibility = str(source.get("visibility") or "private")
        try:
            runtime.client.publish(
                text=text,
                visibility=visibility,
                reply_to_id=source_id,
                media_ids=[],
                idempotency_key=_idempotency_key(bot_id, source_id),
            )
            published = True
        except MastodonApiError as exc:
            _log(f"status {source_id} reply failed: {exc}")
            _audit(runtime, source_id, ok=False, detail="publish_failed")
        runtime.db.worker_mark_done(bot_id, source_id)
        if published:
            _audit(runtime, source_id, ok=True, detail=f"{len(transcript)} chars")
        return published
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            _log(f"could not remove temporary audio {temp_path}")


def _reply_text(transcript: str, max_status_chars: int) -> str:
    text = TRANSCRIPT_PREFIX + transcript
    limit = max(1, int(max_status_chars) - 20)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _idempotency_key(bot_id: str, source_id: str) -> str:
    return hashlib.sha256(f"{bot_id}:{source_id}:transcribe".encode("utf-8")).hexdigest()


def _own_account_id(runtime: Any) -> str:
    try:
        account = runtime.client.verify_credentials() or {}
    except MastodonApiError as exc:
        _log(f"verify_credentials failed, self-reply guard degraded: {exc}")
        return ""
    return str(account.get("id") or "")


def _status_sort_key(raw: dict[str, Any]) -> tuple[int, str]:
    value = str(raw.get("id") or "")
    return (int(value), value) if value.isdigit() else (0, value)


def _is_newer(candidate: str, current: str | None) -> bool:
    if not current:
        return True
    if candidate.isdigit() and current.isdigit():
        return int(candidate) > int(current)
    return candidate > current


def _suffix(url: str) -> str:
    suffix = Path(str(url).split("?", 1)[0].split("#", 1)[0]).suffix
    return suffix if _SUFFIX_RE.fullmatch(suffix) else ".audio"


def _audit(runtime: Any, source_id: str, *, ok: bool, detail: str) -> None:
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    audit("worker", "transcribe", ok=ok, target_id=source_id, detail=detail)


def _log(message: str) -> None:
    print(f"[cmx-worker] {message}", file=sys.stderr, flush=True)


def _bounded_text(name: str, default: str, *, max_chars: int) -> str:
    raw = os.getenv(name)
    value = default if raw is None else raw.strip()
    if len(value) > max_chars:
        raise RuntimeError(f"{name} must be at most {max_chars} characters")
    return value


if __name__ == "__main__":
    main()
