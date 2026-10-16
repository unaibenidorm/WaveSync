"""WaveSync core package — toolkit-independent (zero ``gi``/GTK imports)."""

from __future__ import annotations

from wavesync.core.errors import (
    AnalysisError,
    Cancelled,
    CancelToken,
    ExportError,
    FFmpegError,
    FFmpegNotFoundError,
    MediaError,
    WaveSyncError,
)
from wavesync.core.export import ExportJob, VerificationReport, export, verify_output
from wavesync.core.ffmpeg import FFmpegBinaries, binaries, locate_binaries
from wavesync.core.languages import (
    ALL_LANGUAGES,
    Language,
    find_language,
    normalize_language_code,
    search_languages,
)
from wavesync.core.models import (
    AudioStream,
    Container,
    ExportTrack,
    MediaInfo,
    SyncResult,
    SyncSegment,
    SyncWindow,
    TempoMode,
    Verdict,
    VideoStream,
)
from wavesync.core.probe import probe, require_audio
from wavesync.core.sync import SyncParams, analyze, analyze_with
from wavesync.core.tempo import PRESETS, TempoPreset, preset_by_key, snap_to_preset

__all__ = [
    "ALL_LANGUAGES",
    "AnalysisError",
    "AudioStream",
    "CancelToken",
    "Cancelled",
    "Container",
    "ExportError",
    "ExportJob",
    "ExportTrack",
    "FFmpegBinaries",
    "FFmpegError",
    "FFmpegNotFoundError",
    "Language",
    "MediaError",
    "MediaInfo",
    "PRESETS",
    "SyncParams",
    "SyncResult",
    "SyncSegment",
    "SyncWindow",
    "TempoMode",
    "TempoPreset",
    "Verdict",
    "VerificationReport",
    "VideoStream",
    "WaveSyncError",
    "analyze",
    "analyze_with",
    "binaries",
    "export",
    "find_language",
    "locate_binaries",
    "normalize_language_code",
    "preset_by_key",
    "probe",
    "require_audio",
    "search_languages",
    "snap_to_preset",
    "verify_output",
]

