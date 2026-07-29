"""Put browser-recorded audio into a container Mastodon will accept.

MediaRecorder can only produce WebM or MP4. Both are *video* containers, so the
`file` magic Mastodon runs reports video/webm and video/mp4 no matter that the
file holds nothing but audio. That costs an upload either way:

  * declared audio/webm -> detected video/webm -> Paperclip spoof check -> 422
  * declared audio/mp4  -> typed as video      -> "Video has no video stream"

Ogg is unambiguous: its magic is audio/ogg, and Opus — what Chrome and Firefox
already record — is Ogg's native codec, so the common case is a container swap
with no re-encode and no quality loss. iOS Safari records AAC instead, which has
no place in Ogg, so that one path is decoded and re-encoded to Opus.

PyAV ships its own ffmpeg libraries and is already present as a faster-whisper
dependency, so this adds nothing to install.
"""

from __future__ import annotations

from pathlib import Path

OGG_MIME = "audio/ogg"
OGG_SUFFIX = ".ogg"
OPUS_BITRATE = 64_000


class VoiceMediaError(RuntimeError):
    """Raised when the upload is not audio we can put into Ogg."""


def to_ogg_opus(source: str | Path, target: str | Path) -> dict[str, object]:
    """Rewrite `source` as Ogg/Opus at `target`.

    Returns what happened so the caller can log it without re-opening the file.
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
            copied = codec == "opus"

            with av.open(target, "w", format="ogg") as out:
                if copied:
                    # Same codec, different wrapper: no decode, no loss.
                    out_stream = out.add_stream_from_template(in_stream)
                    for packet in inp.demux(in_stream):
                        if packet.dts is None:
                            continue
                        packet.stream = out_stream
                        out.mux(packet)
                else:
                    out_stream = out.add_stream("libopus", rate=48_000)
                    out_stream.bit_rate = OPUS_BITRATE
                    for frame in inp.decode(in_stream):
                        frame.pts = None
                        for packet in out_stream.encode(frame):
                            out.mux(packet)
                    for packet in out_stream.encode(None):
                        out.mux(packet)
    except VoiceMediaError:
        raise
    except Exception as exc:
        raise VoiceMediaError(f"remux_failed: {type(exc).__name__}: {exc}"[:200]) from exc

    size = Path(target).stat().st_size if Path(target).exists() else 0
    if size < 1:
        raise VoiceMediaError("remux_produced_nothing")
    return {"source_codec": codec, "copied": copied, "size_bytes": size}
