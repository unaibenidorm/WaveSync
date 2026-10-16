"""Typed data classes exchanged between core and UI.

Time convention used everywhere (see ``AGENTS.md``):

* All times are seconds relative to the *start of each file* (format
  ``start_time`` subtracted).
* ``tempo`` is the **speed multiplier applied to audio B**. ``tempo < 1``
  slows B down (e.g. PAL 25 fps → 23.976 fps uses ``0.959041``).
* ``offset`` (seconds): a sound located at ``t_B`` in file B must be heard at
  ``t_A = t_B / tempo + offset`` in the master A. ``offset > 0`` means B
  starts later (silence is prepended); ``offset < 0`` means the beginning of
  B is cut.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from wavesync.core.i18n import _


@dataclass(frozen=True, slots=True)
class AudioStream:
    """One audio stream of a media file, as reported by ffprobe."""

    index: int  #: absolute stream index in the container (``-map 0:<index>``)
    position: int  #: 0-based position among audio streams (``a:<position>``)
    codec: str
    channels: int
    channel_layout: str
    sample_rate: int
    language: str = ""
    title: str = ""
    duration: float | None = None
    bit_rate: int | None = None
    default: bool = False

    @property
    def layout(self) -> str:
        """Channel layout, derived from the channel count when ffprobe gives none."""
        if self.channel_layout:
            return self.channel_layout
        return {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 6: "5.1", 8: "7.1"}.get(
            self.channels, f"{self.channels}c"
        )

    def describe(self) -> str:
        """Short one-line human description."""
        parts = [f"#{self.position + 1}", self.codec.upper(), self.layout]
        if self.language:
            parts.append(self.language)
        if self.title:
            parts.append(f"«{self.title}»")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class VideoStream:
    """Main video stream information."""

    index: int
    codec: str
    width: int
    height: int
    fps_real: float  #: ``r_frame_rate``
    fps_avg: float  #: ``avg_frame_rate``
    hdr: bool
    pix_fmt: str = ""
    color_transfer: str = ""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Result of probing a file with ffprobe."""

    path: Path
    duration: float
    format_name: str
    start_time: float
    audio_streams: tuple[AudioStream, ...]
    video: VideoStream | None
    subtitle_count: int = 0
    chapter_count: int = 0
    size_bytes: int = 0

    @property
    def has_video(self) -> bool:
        """``True`` if the file has a real (non cover-art) video stream."""
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        """``True`` if at least one audio stream exists."""
        return bool(self.audio_streams)


class Verdict(enum.Enum):
    """Global verdict of an analysis."""

    SIMPLE = "simple"
    SEGMENTS = "segments"
    UNRELIABLE = "unreliable"

    @property
    def label(self) -> str:
        """Localised label."""
        return {
            Verdict.SIMPLE: _("Sincronización simple fiable"),
            Verdict.SEGMENTS: _("Requiere tramos"),
            Verdict.UNRELIABLE: _("No fiable, revisar manualmente"),
        }[self]


class TempoMode(enum.Enum):
    """How a tempo change is applied to audio B."""

    ATEMPO = "atempo"  #: time-stretch, keeps pitch
    ASETRATE = "asetrate"  #: resample, pitch changes (undo PAL speed-up)


@dataclass(slots=True)
class SyncWindow:
    """Measurement in one analysis window of the master.

    Offsets follow the global convention (``t_A = t_B / tempo + offset``),
    already corrected for the final tempo.
    """

    t_a: float  #: window start in A (s)
    duration: float
    video_offset: float | None = None
    video_conf: float = 0.0
    audio_offset: float | None = None
    audio_conf: float = 0.0
    inlier: bool = False
    segment: int | None = None

    @property
    def center(self) -> float:
        """Window centre in A (s)."""
        return self.t_a + self.duration / 2

    @property
    def offset(self) -> float | None:
        """Best available offset (audio if accepted, else video)."""
        if self.audio_offset is not None:
            return self.audio_offset
        return self.video_offset

    @property
    def confidence(self) -> float:
        """Confidence of :attr:`offset`."""
        if self.audio_offset is not None:
            return max(self.audio_conf, self.video_conf)
        return self.video_conf


@dataclass(frozen=True, slots=True)
class SyncSegment:
    """A stretch of the master with constant offset.

    ``offset is None`` marks a *gap*: material of A without equivalent in B
    (filled with silence on export).
    """

    start: float  #: in A (s)
    end: float  #: in A (s)
    offset: float | None
    confidence: float = 0.0
    n_windows: int = 0

    @property
    def a_start(self) -> float:
        """Alias for :attr:`start` (time in master A)."""
        return self.start

    @property
    def a_end(self) -> float:
        """Alias for :attr:`end` (time in master A)."""
        return self.end

    @property
    def is_gap(self) -> bool:
        """``True`` if there is no B material for this stretch."""
        return self.offset is None

    @property
    def offset_ms(self) -> float | None:
        """Offset in milliseconds."""
        return None if self.offset is None else self.offset * 1000.0


@dataclass(slots=True)
class SyncResult:
    """Outcome of the automatic synchronisation."""

    offset: float  #: main offset (s), of the longest segment
    tempo: float  #: chosen tempo (possibly snapped to a preset)
    tempo_measured: float
    tempo_preset: str | None
    segments: list[SyncSegment]
    windows: list[SyncWindow]
    confidence: float
    verdict: Verdict
    mode: str  #: "video" or "audio"
    audio_refined: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def offset_ms(self) -> float:
        """Main offset in milliseconds."""
        return self.offset * 1000.0


@dataclass(frozen=True, slots=True)
class ExportTrack:
    """A B audio stream to add to the output, with its metadata."""

    stream: AudioStream
    language: str = "und"
    title: str = ""
    default: bool = False
    source: MediaInfo | None = None


class Container(enum.Enum):
    """Output container."""

    MKV = "mkv"
    MP4 = "mp4"


@dataclass(slots=True)
class ExportJob:
    """Everything needed to write the output file."""

    master: MediaInfo
    source: MediaInfo
    output: Path
    tracks: list[ExportTrack]
    offset: float = 0.0
    tempo: float = 1.0
    tempo_mode: TempoMode = TempoMode.ATEMPO
    segments: list[SyncSegment] | None = None  #: ``None`` → single segment with ``offset``
    codec: str = "aac"  #: aac, ac3, eac3, flac, opus or "copy"
    bitrate_kbps: int | None = None  #: ``None`` → codec default per channel count
    container: Container = Container.MKV
    keep_original_audio: bool = True
    master_audio_indices: tuple[int, ...] | None = None
    subtitle_paths: tuple[Path, ...] = ()
    overwrite: bool = False
