"""Re-encode browser-recorded audio into something both ends will accept.

Two constraints, and only one format satisfies both.

Mastodon inspects magic bytes. MediaRecorder can only emit WebM or MP4, and both
are *video* containers, so a pure-audio file reads as video either way:

  * declared audio/webm -> detected video/webm -> Paperclip spoof check -> 422
  * declared audio/mp4  -> typed as video      -> "Video has no video stream"

Ogg/Opus clears that — its magic is audio/ogg — but every browser on iOS runs
WebKit, and WebKit will not play an Ogg container. Converting for the server's
sake made the recordings unplayable on the phone they were made on.

MP3 is the intersection: `audio/mpeg` by magic, so Mastodon files it as audio,
and there is no browser left that cannot decode it. It costs a real re-encode —
Opus cannot be rewrapped into MP3 — but the widget releases its UI before this
runs, so the time is spent off the critical path.

PyAV ships its own ffmpeg (with libmp3lame) and is already a faster-whisper
dependency, so this adds nothing to install.
"""

from __future__ import annotations

from pathlib import Path

MP3_MIME = "audio/mpeg"
MP3_SUFFIX = ".mp3"
MP3_BITRATE = 64_000        # mono speech; well past transparent for voice
MP3_RATE = 44_100           # libmp3lame's safest sample rate
MP3_LAYOUT = "mono"


class VoiceMediaError(RuntimeError):
    """Raised when the upload is not audio we can convert."""


def to_mp3(
    source: str | Path, target: str | Path, *, max_seconds: float | None = None
) -> dict[str, object]:
    """Rewrite `source` as mono MP3 at `target`.

    ``max_seconds`` stops the rewrite after that much audio, leaving a shorter
    clip; the default keeps the whole file. The voice observer uses it to cap
    the clip it hands Gemini so a long note stays inside the request timeout.

    Returns what happened so the caller can log it without reopening the file.
    """
    try:
        import av
    except Exception as exc:  # pragma: no cover - PyAV ships with faster-whisper
        raise VoiceMediaError(f"pyav_unavailable: {exc}") from exc

    source, target = str(source), str(target)
    try:
        with av.open(source) as inp:
            if not inp.streams.audio:
                raise VoiceMediaError("no_audio_stream")
            in_stream = inp.streams.audio[0]
            codec = str(in_stream.codec_context.name or "")

            with av.open(target, "w", format="mp3") as out:
                out_stream = out.add_stream("libmp3lame", rate=MP3_RATE)
                out_stream.bit_rate = MP3_BITRATE
                try:
                    out_stream.layout = MP3_LAYOUT
                except Exception:
                    # Older PyAV exposes this only through the codec context.
                    out_stream.codec_context.layout = MP3_LAYOUT

                # Opus decodes to 48 kHz float planar; libmp3lame wants its own
                # rate, layout and sample format, so resample rather than hope.
                resampler = av.AudioResampler(
                    format=out_stream.format, layout=MP3_LAYOUT, rate=MP3_RATE
                )
                for frame in inp.decode(in_stream):
                    if max_seconds is not None:
                        frame_time = frame.time
                        if frame_time is not None and frame_time > max_seconds:
                            break
                    for resampled in resampler.resample(frame):
                        resampled.pts = None
                        for packet in out_stream.encode(resampled):
                            out.mux(packet)
                for resampled in resampler.resample(None):
                    resampled.pts = None
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)
                for packet in out_stream.encode(None):
                    out.mux(packet)
    except VoiceMediaError:
        raise
    except Exception as exc:
        raise VoiceMediaError(f"encode_failed: {type(exc).__name__}: {exc}"[:200]) from exc

    size = Path(target).stat().st_size if Path(target).exists() else 0
    if size < 1:
        raise VoiceMediaError("encode_produced_nothing")
    return {"source_codec": codec, "size_bytes": size}
