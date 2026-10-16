"""Batch synchronization and export without any GTK dependency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wavesync.core.errors import CancelToken, Cancelled, MediaError, WaveSyncError
from wavesync.core.export import VerificationReport, export
from wavesync.core.i18n import _
from wavesync.core.models import Container, ExportJob, ExportTrack, TempoMode
from wavesync.core.probe import probe, require_audio
from wavesync.core.sync import SyncParams, analyze

ProgressCb = Callable[[float, str], None]


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One master/source pair and the explicitly selected audio streams."""

    master: Path
    source: Path
    output: Path
    master_track: int = 0
    source_track: int = 0
    language: str = "und"
    title: str = ""


@dataclass(slots=True)
class BatchResult:
    """The result for one processed batch item."""

    item: BatchItem
    report: VerificationReport | None = None
    error: str | None = None


@dataclass(slots=True)
class BatchOptions:
    """Settings shared by all items in a batch."""

    params: SyncParams = field(default_factory=SyncParams)
    codec: str = "aac"
    bitrate_kbps: int | None = None
    tempo_mode: TempoMode = TempoMode.ATEMPO
    keep_original_audio: bool = True
    overwrite: bool = False


def process_batch(
    items: list[BatchItem],
    options: BatchOptions | None = None,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> list[BatchResult]:
    """Synchronize and export items sequentially, keeping later jobs after failures."""
    if not items:
        return []
    settings = options or BatchOptions()
    results: list[BatchResult] = []
    total = len(items)

    for index, item in enumerate(items):
        if cancel:
            cancel.raise_if_cancelled()
        prefix = f"{index + 1}/{total}"
        try:
            if on_progress:
                on_progress(index / total, f"[{prefix}] Analizando {item.master.name}")
            master = probe(item.master, cancel=cancel)
            source = probe(item.source, cancel=cancel)
            require_audio(source)
            if not source.audio_streams:
                raise MediaError(_("La fuente no contiene audio: {path}").format(path=item.source))
            if not 0 <= item.source_track < len(source.audio_streams):
                raise MediaError(
                    _("Pista B inválida ({track}) para {name}; hay {count} pista(s).").format(
                        track=item.source_track,
                        name=item.source.name,
                        count=len(source.audio_streams),
                    )
                )
            if master.audio_streams and not 0 <= item.master_track < len(master.audio_streams):
                raise MediaError(
                    _("Pista A inválida ({track}) para {name}; hay {count} pista(s).").format(
                        track=item.master_track,
                        name=item.master.name,
                        count=len(master.audio_streams),
                    )
                )
            master_stream = master.audio_streams[item.master_track] if master.audio_streams else None
            source_stream = source.audio_streams[item.source_track]

            result = analyze(
                master,
                source,
                master_stream,
                source_stream,
                params=settings.params,
                cancel=cancel,
            )
            job = ExportJob(
                master=master,
                source=source,
                output=item.output,
                tracks=[ExportTrack(source_stream, item.language, item.title, True)],
                offset=result.offset,
                tempo=result.tempo,
                tempo_mode=settings.tempo_mode,
                segments=result.segments if len(result.segments) > 1 else None,
                codec=settings.codec,
                bitrate_kbps=settings.bitrate_kbps,
                container=Container.MP4 if item.output.suffix.lower() == ".mp4" else Container.MKV,
                keep_original_audio=settings.keep_original_audio,
                overwrite=settings.overwrite,
            )
            report = export(
                job,
                cancel=cancel,
                on_progress=(
                    (lambda fraction, message: on_progress((index + fraction) / total, f"[{prefix}] {message}"))
                    if on_progress
                    else None
                ),
            )
            results.append(BatchResult(item=item, report=report))
        except Cancelled:
            raise
        except WaveSyncError as exc:
            results.append(BatchResult(item=item, error=exc.user_message))
        if on_progress:
            on_progress((index + 1) / total, f"[{prefix}] Completado {item.master.name}")
    return results
