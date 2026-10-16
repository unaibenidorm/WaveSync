"""Export pipeline: mux master video/audio/subs/chapters + synchronized B tracks.

Key guarantees:

* Video, subtitles, chapters and (optionally) original master audio streams
  are copied bit-for-bit (``-c:v copy -c:s copy``).
* Positive offsets use ``adelay`` (with ``all=1`` so all channels are delayed
  equally); negative offsets use ``atrim=start=...`` + ``asetpts``. We never
  rely on negative ``-itsoffset``.
* When ``codec="copy"`` is requested and valid (single segment, ``tempo == 1``,
  ``offset >= 0``), positive ``-itsoffset`` is used; if ``offset < 0`` and
  copy is forced, input ``-ss`` is used with a warning about codec-frame
  granularity.
* Multi-segment timelines build the track with ``atrim``/``anullsrc``/``concat``
  and apply tempo before trimming on the master axis.
* Output is written to a hidden temporary file in the target directory and
  atomically renamed via :func:`os.replace` on success; cleaned up on error
  or cancellation.
* After muxing, :func:`verify_output` runs ``ffprobe`` to check stream counts,
  language/title metadata, duration, and that the video stream was copied
  without re-encoding.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wavesync.core.errors import CancelToken, ExportError
from wavesync.core.ffmpeg import binaries, fmt_time
from wavesync.core.i18n import _
from wavesync.core.languages import normalize_language_code
from wavesync.core.models import (
    AudioStream,
    Container,
    ExportJob,
    MediaInfo,
    SyncSegment,
    TempoMode,
)
from wavesync.core.probe import probe

ProgressCb = Callable[[float, str], None]

SUPPORTED_CODECS: dict[str, str] = {
    "aac": "aac",
    "ac3": "ac3",
    "eac3": "eac3",
    "flac": "flac",
    "opus": "libopus",
    "copy": "copy",
}

MP4_UNSAFE_CODECS = {"flac"}


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Result of verifying the exported file with ffprobe."""

    info: MediaInfo
    video_copied: bool
    audio_count_ok: bool
    duration_ok: bool
    metadata_ok: bool
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """``True`` when all mandatory checks passed."""
        return self.video_copied and self.audio_count_ok and self.duration_ok and self.metadata_ok


def default_bitrate_kbps(codec: str, channels: int) -> int | None:
    """Reasonable default bitrate (kbps) per codec and channel count."""
    if codec in ("flac", "copy"):
        return None
    ch = max(1, channels)
    if codec == "aac":
        return 96 if ch == 1 else (192 if ch == 2 else 384)
    if codec == "opus":
        return 64 if ch == 1 else (128 if ch == 2 else 256)
    if codec == "ac3":
        return 192 if ch <= 2 else 448
    if codec == "eac3":
        return 192 if ch <= 2 else 640
    return 192


def output_sample_rate(codec: str, input_sample_rate: int) -> int:
    """Choose a broadly compatible sample rate for newly encoded audio."""
    if codec in {"aac", "opus"}:
        return 48000
    return input_sample_rate or 48000


def atempo_chain(tempo: float) -> list[str]:
    """Decompose ``tempo`` into a chain of ``atempo`` filters (each in ``[0.5, 2.0]``)."""
    filters: list[str] = []
    rem = float(tempo)
    while rem < 0.5 - 1e-9:
        filters.append("atempo=0.5")
        rem /= 0.5
    while rem > 2.0 + 1e-9:
        filters.append("atempo=2.0")
        rem /= 2.0
    if abs(rem - 1.0) > 1e-7:
        filters.append(f"atempo={rem:.8f}")
    return filters


def tempo_filters(tempo: float, mode: TempoMode, sample_rate: int) -> list[str]:
    """FFmpeg audio filter list to apply ``tempo`` to a stream."""
    if abs(tempo - 1.0) <= 1e-7:
        return []
    sr = sample_rate or 48000
    if mode == TempoMode.ASETRATE:
        return [
            f"asetrate={sr * tempo:.6f}",
            f"aresample={sr}:async=1",
        ]
    return atempo_chain(tempo)


def _channel_format_filter(stream: AudioStream, codec: str) -> str | None:
    """Keep the source channel layout when encoding, normalizing surround variants."""
    layout = stream.layout
    if not layout or layout.endswith("c"):
        return None
    # Normalize surround layouts across all encoders to avoid PCE in AAC
    # (which produces unplayable silence on most consumer decoders)
    # and unhandled side/back channel permutations in AC-3/E-AC-3/Opus.
    if layout in ("5.1(side)", "5.1(back)"):
        layout = "5.1"
    elif layout in ("7.1(wide)", "7.1(side)"):
        layout = "7.1"
    elif layout == "quad(side)":
        layout = "quad"
    return f"aformat=channel_layouts={layout}"


def _single_segment_filter(
    input_spec: str,
    out_label: str,
    stream: AudioStream,
    offset: float,
    tempo: float,
    tempo_mode: TempoMode,
    master_duration: float,
    codec: str,
) -> str:
    """Build a filtergraph chain for a single constant-offset track."""
    # Input streams can carry a non-zero start timestamp (common in UHD
    # remuxes). The synchronisation model is relative to each file's start,
    # so discard that container timestamp before applying the offset.
    chain: list[str] = ["aresample=async=1:first_pts=0", "asetpts=PTS-STARTPTS"]
    if offset < -1e-6:
        # Trim on B's source timeline before changing its speed: at A=0,
        # B must start at (-offset) * tempo according to the time convention.
        chain.append(f"atrim=start={fmt_time(-offset * tempo)}")
        chain.append("asetpts=PTS-STARTPTS")
    chain.extend(tempo_filters(tempo, tempo_mode, stream.sample_rate))
    sample_rate = output_sample_rate(codec, stream.sample_rate)
    if sample_rate != stream.sample_rate:
        chain.append(f"aresample={sample_rate}")
    # atempo/asetrate can preserve source frame timestamps; reset again
    # before adding a delay so the delay is measured from B=0.
    chain.append("asetpts=PTS-STARTPTS")
    if offset > 1e-6:
        ms = offset * 1000.0
        chain.append(f"adelay={ms:.3f}:all=1")
        chain.append("asetpts=PTS-STARTPTS")
    if master_duration > 0:
        # ``atrim`` after ``adelay`` can reorder delayed audio frames when
        # the stream has first been tempo-converted. ``whole_dur`` pads to
        # the master duration without rewriting the delayed timestamps.
        chain.append(f"apad=whole_dur={fmt_time(master_duration)}")
        chain.append("asetpts=N/SR/TB")
    else:
        chain.append("asetpts=N/SR/TB")
    cf = _channel_format_filter(stream, codec)
    if cf:
        chain.append(cf)
    return f"[{input_spec}]{','.join(chain)}[{out_label}]"


def _multi_segment_filter(
    input_spec: str,
    out_label: str,
    stream: AudioStream,
    segments: list[SyncSegment],
    tempo: float,
    tempo_mode: TempoMode,
    master_duration: float,
    codec: str,
    prefix: str,
) -> str:
    """Build a filtergraph that assembles ``segments`` with ``atrim``/``anullsrc``/``concat``."""
    sr = stream.sample_rate or 48000
    layout = stream.layout if not stream.layout.endswith("c") else "stereo"
    if layout in ("5.1(side)", "5.1(back)"):
        layout = "5.1"
    elif layout in ("7.1(wide)", "7.1(side)"):
        layout = "7.1"
    elif layout == "quad(side)":
        layout = "quad"
    parts: list[str] = []
    seg_labels: list[str] = []
    t_filters = tempo_filters(tempo, tempo_mode, sr)
    for idx, seg in enumerate(segments):
        dur = max(0.0, seg.end - seg.start)
        if dur < 1e-4:
            continue
        lbl = f"{prefix}_s{idx}"
        seg_labels.append(f"[{lbl}]")
        if seg.is_gap or seg.offset is None:
            parts.append(
                f"anullsrc=r={sr}:cl={layout},atrim=0:{fmt_time(dur)},asetpts=N/SR/TB[{lbl}]"
            )
            continue
        # Map the master interval to B before applying tempo. Trimming the
        # source after tempo incorrectly treats master seconds as B seconds.
        b_start = (seg.start - seg.offset) * tempo
        b_end = (seg.end - seg.offset) * tempo
        # Work on the source's relative timeline. Without this reset, a
        # non-zero stream start_pts leaks into the concatenated master-axis
        # timeline and makes export differ from the preview.
        chain: list[str] = ["aresample=async=1:first_pts=0", "asetpts=PTS-STARTPTS"]
        if b_end <= 0:
            parts.append(
                f"anullsrc=r={sr}:cl={layout},atrim=0:{fmt_time(dur)},asetpts=N/SR/TB[{lbl}]"
            )
            continue
        if b_start < -1e-6:
            if b_end > 1e-6:
                chain.append(f"atrim=0:{fmt_time(b_end)}")
                chain.append("asetpts=PTS-STARTPTS")
            chain.extend(t_filters)
            chain.append("asetpts=PTS-STARTPTS")
            chain.append(f"adelay={(-b_start / tempo) * 1000.0:.3f}:all=1")
        else:
            chain.append(f"atrim=start={fmt_time(b_start)}:end={fmt_time(b_end)}")
            chain.append("asetpts=PTS-STARTPTS")
            chain.extend(t_filters)
        chain.append(f"apad=whole_dur={fmt_time(dur)}")
        chain.append("asetpts=N/SR/TB")
        parts.append(f"[{input_spec}]{','.join(chain)}[{lbl}]")
    if not seg_labels:
        return _single_segment_filter(
            input_spec, out_label, stream, 0.0, tempo, tempo_mode, master_duration, codec
        )
    tail = f"concat=n={len(seg_labels)}:v=0:a=1"
    if master_duration > 0:
        tail += f",apad=whole_dur={fmt_time(master_duration)},asetpts=N/SR/TB"
    else:
        tail += ",asetpts=N/SR/TB"
    sample_rate = output_sample_rate(codec, stream.sample_rate)
    if sample_rate != stream.sample_rate:
        tail += f",aresample={sample_rate}"
    cf = _channel_format_filter(stream, codec)
    if cf:
        tail += f",{cf}"
    parts.append(f"{''.join(seg_labels)}{tail}[{out_label}]")
    return ";".join(parts)


def can_direct_copy(job: ExportJob) -> bool:
    """Whether ``job`` can copy the B audio stream without re-encoding."""
    if abs(job.tempo - 1.0) > 1e-7:
        return False
    if job.segments is not None:
        real = [s for s in job.segments if not s.is_gap]
        if len(real) != 1 or len(job.segments) != 1:
            return False
    return True


def _can_copy_requested_codec(job: ExportJob, codec_key: str) -> bool:
    """Only use direct packet copy when explicitly requested via codec='copy'."""
    return codec_key == "copy" and can_direct_copy(job)


def build_ffmpeg_command(job: ExportJob, temp_output: Path) -> tuple[list[str], list[str]]:
    """Construct the ``ffmpeg`` command line for ``job`` and return ``(args, warnings)``."""
    selected_master_audio = (
        tuple(range(len(job.master.audio_streams)))
        if job.master_audio_indices is None and job.keep_original_audio
        else tuple(job.master_audio_indices or ())
    )
    valid_master_positions = [
        pos for pos in selected_master_audio
        if 0 <= pos < len(job.master.audio_streams)
    ]
    if not job.tracks and not valid_master_positions:
        raise ExportError(_("Debes seleccionar al menos una pista de audio (del máster o de la fuente B) para exportar."))
    codec_key = job.codec.lower()
    if codec_key not in SUPPORTED_CODECS:
        raise ExportError(_("Códec de audio no soportado: {c}").format(c=job.codec))
    warnings: list[str] = []
    if job.container == Container.MP4:
        warnings.append(
            _("El contenedor MP4 tiene soporte limitado para subtítulos (p. ej. SRT/ASS), "
              "capítulos y algunos códecs de audio. Se recomienda MKV.")
        )
        if codec_key in MP4_UNSAFE_CODECS:
            raise ExportError(
                _("El códec {c} no es compatible de forma estándar con MP4; usa MKV o elige AAC/AC3/Opus.").format(
                    c=codec_key.upper()
                )
            )
    direct_copy = _can_copy_requested_codec(job, codec_key)
    if codec_key == "copy" and not direct_copy:
        warnings.append(
            _("No es posible copiar el audio sin recodificar cuando hay cambio de tempo o múltiples tramos; "
              "se recodificará en AAC.")
        )
        direct_copy = False
        codec_key = "aac"

    eff_offset = job.offset
    multi_segs: list[SyncSegment] | None = None
    if job.segments is not None:
        real = [s for s in job.segments if not s.is_gap]
        if len(job.segments) > 1 and len(real) > 1:
            multi_segs = job.segments

    args: list[str] = [
        str(binaries().ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts+discardcorrupt",
        "-progress",
        "pipe:1",
        "-i",
        str(job.master.path),
    ]

    # Collect all unique sources
    unique_sources: list[MediaInfo] = [job.source]
    for tr in job.tracks:
        src = tr.source or job.source
        if src.path.resolve() not in [s.path.resolve() for s in unique_sources]:
            unique_sources.append(src)

    source_input_map: dict[Path, int] = {}
    for i, src in enumerate(unique_sources):
        src_idx = 1 + i
        source_input_map[src.path.resolve()] = src_idx
        if direct_copy:
            if eff_offset >= 0:
                if eff_offset > 1e-6:
                    args += ["-itsoffset", fmt_time(eff_offset)]
                args += ["-i", str(src.path)]
            else:
                if i == 0:
                    warnings.append(
                        _("Copia directa con offset negativo: el recorte inicial tiene la precisión de un fotograma "
                          "del códec de audio (~20–32 ms). Para precisión sub-milisegundo, recodifica la pista.")
                    )
                args += ["-ss", fmt_time(-eff_offset), "-i", str(src.path)]
        else:
            args += ["-i", str(src.path)]

    filter_chains: list[str] = []
    for k, tr in enumerate(job.tracks):
        src = tr.source or job.source
        input_file_idx = source_input_map[src.path.resolve()]
        in_spec = f"{input_file_idx}:{tr.stream.index}"
        if not direct_copy:
            out_lbl = f"ws_a{k}"
            if multi_segs is not None:
                fc = _multi_segment_filter(
                    in_spec,
                    out_lbl,
                    tr.stream,
                    multi_segs,
                    job.tempo,
                    job.tempo_mode,
                    job.master.duration,
                    codec_key,
                    f"t{k}",
                )
            else:
                fc = _single_segment_filter(
                    in_spec,
                    out_lbl,
                    tr.stream,
                    eff_offset,
                    job.tempo,
                    job.tempo_mode,
                    job.master.duration,
                    codec_key,
                )
            filter_chains.append(fc)

    if filter_chains:
        args += ["-filter_complex", ";".join(filter_chains)]

    # Stream mapping from master. Keep video, chapters and attachments, while
    # allowing individual original audio tracks to be removed.
    args += ["-map", "0:v?", "-map", "0:s?", "-map", "0:t?", "-map_chapters", "0"]
    for position in valid_master_positions:
        args += ["-map", f"0:{job.master.audio_streams[position].index}"]
    base_audio_idx = len(valid_master_positions)

    for subtitle_path in job.subtitle_paths:
        args += ["-i", str(subtitle_path), "-map", f"{len(unique_sources) + 1}:s?"]

    for k, tr in enumerate(job.tracks):
        src = tr.source or job.source
        input_file_idx = source_input_map[src.path.resolve()]
        if direct_copy:
            args += ["-map", f"{input_file_idx}:{tr.stream.index}"]
        else:
            args += ["-map", f"[ws_a{k}]"]

    # Copy video, subtitles, chapters, and existing master audio
    args += ["-c:v", "copy"]
    if job.container == Container.MKV:
        args += ["-c:s", "copy"]
    elif job.master.subtitle_count > 0:
        args += ["-c:s", "mov_text"]
    args += ["-map_metadata", "0"]

    for i in range(base_audio_idx):
        args += [f"-c:a:{i}", "copy"]

    any_new_default = any(tr.default for tr in job.tracks)
    if any_new_default:
        for i in range(base_audio_idx):
            args += [f"-disposition:a:{i}", "0"]

    enc_name = "copy" if direct_copy else SUPPORTED_CODECS[codec_key]
    for k, tr in enumerate(job.tracks):
        a_idx = base_audio_idx + k
        args += [f"-c:a:{a_idx}", enc_name]
        if not direct_copy:
            br = job.bitrate_kbps or default_bitrate_kbps(codec_key, tr.stream.channels)
            if br:
                args += [f"-b:a:{a_idx}", f"{br}k"]
            if tr.stream.channels > 0:
                args += [f"-ac:a:{a_idx}", str(tr.stream.channels)]
            args += [f"-ar:a:{a_idx}", str(output_sample_rate(codec_key, tr.stream.sample_rate))]
        lang = normalize_language_code(tr.language)
        args += [f"-metadata:s:a:{a_idx}", f"language={lang}"]
        if tr.title:
            args += [f"-metadata:s:a:{a_idx}", f"title={tr.title}"]
        args += [f"-disposition:a:{a_idx}", "default" if tr.default else "0"]

    if job.subtitle_paths:
        subtitle_start = job.master.subtitle_count
        subtitle_codec = "copy" if job.container == Container.MKV else "mov_text"
        for index, subtitle_path in enumerate(job.subtitle_paths):
            output_index = subtitle_start + index
            args += [
                f"-c:s:{output_index}", subtitle_codec,
                f"-metadata:s:s:{output_index}", f"title={subtitle_path.stem}",
            ]

    muxer = "matroska" if job.container == Container.MKV else "mp4"
    # Do not write an audio stream after a large run of video packets when a
    # filter graph produces one track later than the others. Such files have
    # valid timestamps but can stall Matroska players while seeking audio.
    args += [
        # Zero means "unlimited" to FFmpeg, which makes it queue every UHD
        # video packet until a delayed filtered audio stream catches up.
        # A bounded one-second window keeps the file seekable and prevents
        # mpv/VLC's demuxer queues from growing without limit.
        "-max_interleave_delta", "1000000",
        "-flush_packets", "1",
        # Keep all copied and filtered streams on one common, non-negative
        # output timeline. This is important for Matroska players when the
        # master contains delayed/offset UHD timestamps.
        "-avoid_negative_ts", "make_zero",
        "-t", fmt_time(job.master.duration),
        "-f", muxer,
        str(temp_output),
    ]
    return args, warnings


def export(
    job: ExportJob,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
) -> VerificationReport:
    """Execute ``job`` with real-time progress, atomic rename and post-export verification."""
    cancel = cancel or CancelToken()
    progress = on_progress or (lambda f, m: None)
    out_path = job.output.resolve()
    if out_path in (job.master.path.resolve(), job.source.path.resolve()):
        raise ExportError(_("El archivo de salida no puede ser el mismo que el de entrada."))
    if out_path.exists() and not job.overwrite:
        raise ExportError(
            _("El archivo «{name}» ya existe. Activa la confirmación de sobrescritura o elige otro nombre.").format(
                name=out_path.name
            )
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.parent / f".{out_path.stem}.wavesync-{os.getpid()}.part{out_path.suffix}"
    args, warnings = build_ffmpeg_command(job, temp_path)

    dur_us = max(1.0, job.master.duration * 1_000_000.0)
    progress(0.0, _("Iniciando exportación…"))
    proc: subprocess.Popen[bytes] | None = None
    try:
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ExportError(_("No se pudo iniciar ffmpeg para exportar."), str(exc)) from exc

        assert proc.stdout is not None
        assert proc.stderr is not None
        for raw_line in iter(proc.stdout.readline, b""):
            if cancel.cancelled:
                proc.kill()
                proc.wait()
                cancel.raise_if_cancelled()
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                val = line.split("=", 1)[1]
                if val.lstrip("-").isdigit():
                    us = max(0, int(val))
                    frac = min(0.98, us / dur_us)
                    progress(frac, _("Exportando ({p:.0%})…").format(p=frac))
            elif line == "progress=end":
                break
        _unused_out, stderr_bytes = proc.communicate()
        if cancel.cancelled:
            cancel.raise_if_cancelled()
        if proc.returncode != 0:
            err_txt = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise ExportError(
                _("ffmpeg falló durante la exportación (código {c}).").format(c=proc.returncode),
                err_txt,
            )
        os.replace(temp_path, out_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    progress(0.99, _("Verificando archivo exportado…"))
    report = verify_output(job, out_path, warnings=warnings, cancel=cancel)
    if not report.ok:
        raise ExportError(
            _("El archivo exportado no superó la verificación automática."),
            "\n".join(report.warnings),
        )
    progress(1.0, _("Exportación completada y verificada."))
    return report


def verify_output(
    job: ExportJob,
    output_path: Path,
    *,
    warnings: list[str] | None = None,
    cancel: CancelToken | None = None,
) -> VerificationReport:
    """Verify the exported file with ``ffprobe``."""
    notes = list(warnings or [])
    info = probe(output_path, cancel=cancel)

    # 1. Video copied without re-encoding
    if job.master.video is not None:
        v_in = job.master.video
        v_out = info.video
        video_copied = (
            v_out is not None
            and v_out.codec == v_in.codec
            and v_out.width == v_in.width
            and v_out.height == v_in.height
            and v_out.pix_fmt == v_in.pix_fmt
        )
        if not video_copied:
            notes.append(_("El stream de vídeo de salida no coincide con el del máster."))
    else:
        video_copied = True

    # 2. Expected number of audio tracks
    selected_master_audio = (
        tuple(range(len(job.master.audio_streams)))
        if job.master_audio_indices is None and job.keep_original_audio
        else tuple(job.master_audio_indices or ())
    )
    base = sum(
        1 for pos in selected_master_audio
        if 0 <= pos < len(job.master.audio_streams)
    )
    expected_audio = base + len(job.tracks)
    audio_count_ok = len(info.audio_streams) == expected_audio
    if not audio_count_ok:
        notes.append(
            _("Se esperaban {e} pistas de audio y se encontraron {f}.").format(
                e=expected_audio, f=len(info.audio_streams)
            )
        )

    # 3. Duration within tolerance of master
    tol = max(1.5, 0.02 * job.master.duration)
    duration_ok = abs(info.duration - job.master.duration) <= tol
    if not duration_ok:
        notes.append(
            _("Duración de salida ({d:.2f} s) difiere de la del máster ({m:.2f} s).").format(
                d=info.duration, m=job.master.duration
            )
        )

    # 4. Language & title metadata on added tracks
    metadata_ok = True
    if audio_count_ok:
        for k, tr in enumerate(job.tracks):
            a_out = info.audio_streams[base + k]
            exp_lang = normalize_language_code(tr.language)
            if exp_lang != "und" and a_out.language.lower() != exp_lang.lower():
                metadata_ok = False
                notes.append(
                    _("Idioma en pista #{i}: esperado «{e}», leído «{f}».").format(
                        i=base + k + 1, e=exp_lang, f=a_out.language
                    )
                )
            if tr.title and a_out.title != tr.title:
                metadata_ok = False
                notes.append(
                    _("Título en pista #{i}: esperado «{e}», leído «{f}».").format(
                        i=base + k + 1, e=tr.title, f=a_out.title
                    )
                )

    return VerificationReport(
        info=info,
        video_copied=video_copied,
        audio_count_ok=audio_count_ok,
        duration_ok=duration_ok,
        metadata_ok=metadata_ok,
        warnings=tuple(notes),
    )
