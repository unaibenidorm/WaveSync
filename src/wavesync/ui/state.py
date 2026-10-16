"""Shared application state and observer callbacks for the GTK4 UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from wavesync.core.models import (
    AudioStream,
    Container,
    MediaInfo,
    SyncResult,
    SyncSegment,
    TempoMode,
    Verdict,
)
from wavesync.core.sync import SyncParams


@dataclass
class TrackSelection:
    """User configuration for one B audio track to export."""

    stream: AudioStream
    enabled: bool = True
    language: str = "und"
    title: str = ""
    default: bool = False
    source: MediaInfo | None = None


@dataclass
class BatchEntry:
    """State for one (Master A, Source B) pair in batch mode."""

    master_path: Path
    source_path: Path
    master: MediaInfo | None = None
    source: MediaInfo | None = None
    master_audio_idx: int = 0
    source_audio_idx: int = 0
    result: SyncResult | None = None
    offset_ms: float = 0.0
    tempo: float = 1.0
    tempo_mode: TempoMode = TempoMode.ATEMPO
    use_segments: bool = False
    segments: list[SyncSegment] = field(default_factory=list)
    track_configs: list[TrackSelection] = field(default_factory=list)
    master_audio_indices: tuple[int, ...] | None = None
    error: str | None = None

    @property
    def active_segments(self) -> list[SyncSegment] | None:
        real = [s for s in self.segments if not s.is_gap]
        if self.use_segments and len(real) > 1:
            return self.segments
        return None


def _default_track_configs(info: MediaInfo | None, active_idx: int = 0) -> list[TrackSelection]:
    configs: list[TrackSelection] = []
    if info and info.audio_streams:
        for i, s in enumerate(info.audio_streams):
            configs.append(
                TrackSelection(
                    stream=s,
                    enabled=(i == active_idx),
                    language=s.language or "spa",
                    title=s.title or "",
                    default=(i == active_idx),
                    source=info,
                )
            )
    return configs


@dataclass
class AppState:
    """Central UI state shared across views."""

    master: MediaInfo | None = None
    source: MediaInfo | None = None
    master_audio_idx: int = 0
    source_audio_idx: int = 0

    params: SyncParams = field(default_factory=SyncParams)
    result: SyncResult | None = None

    # Active (possibly manually calibrated) parameters
    offset_ms: float = 0.0
    tempo: float = 1.0
    tempo_mode: TempoMode = TempoMode.ATEMPO
    use_segments: bool = False
    segments: list[SyncSegment] = field(default_factory=list)

    # Export track metadata & options
    track_configs: list[TrackSelection] = field(default_factory=list)
    codec: str = "aac"
    bitrate_kbps: int = 192
    container: Container = Container.MKV
    keep_original_audio: bool = True
    master_audio_indices: tuple[int, ...] | None = None

    # UI complexity; simple mode is deliberately the default.
    expert_mode: bool = False
    sync_in_progress: bool = False
    batch_enabled: bool = False
    batch_masters: list = field(default_factory=list)
    batch_sources: list = field(default_factory=list)
    batch_entries: list[BatchEntry] = field(default_factory=list)
    active_batch_idx: int = 0

    _listeners: list[Callable[[str], None]] = field(default_factory=list, repr=False)

    def subscribe(self, cb: Callable[[str], None]) -> None:
        """Register a listener called with ``topic`` whenever state changes."""
        self._listeners.append(cb)

    def notify(self, topic: str = "all") -> None:
        """Notify all listeners of a state update."""
        for cb in list(self._listeners):
            cb(topic)

    def set_expert_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.expert_mode != enabled:
            self.expert_mode = enabled
            self.notify("preferences")

    def set_sync_in_progress(self, active: bool) -> None:
        active = bool(active)
        if self.sync_in_progress != active:
            self.sync_in_progress = active
            self.notify("syncing")

    @property
    def master_stream(self) -> AudioStream | None:
        if self.master and self.master.audio_streams:
            idx = min(max(0, self.master_audio_idx), len(self.master.audio_streams) - 1)
            return self.master.audio_streams[idx]
        return None

    @property
    def source_stream(self) -> AudioStream | None:
        if self.source and self.source.audio_streams:
            idx = min(max(0, self.source_audio_idx), len(self.source.audio_streams) - 1)
            return self.source.audio_streams[idx]
        return None

    @property
    def offset_seconds(self) -> float:
        return self.offset_ms / 1000.0

    @property
    def active_segments(self) -> list[SyncSegment] | None:
        segs = self.segments if self.segments else (self.result.segments if self.result else [])
        real = [s for s in segs if not s.is_gap]
        if self.use_segments and len(real) > 1:
            return segs
        return None

    def set_offset_ms(self, new_offset_ms: float, *, notify_topic: str | None = "calibration") -> None:
        """Update ``offset_ms`` and shift any active multi-segment offsets by the same delta."""
        new_val = float(new_offset_ms)
        delta_s = (new_val - self.offset_ms) / 1000.0
        self.offset_ms = new_val
        if abs(delta_s) > 1e-6 and self.segments:
            self.segments = [
                replace(s, offset=s.offset + delta_s) if not s.is_gap else s
                for s in self.segments
            ]
        if self.batch_enabled:
            self.save_active_to_batch_entry()
        if notify_topic:
            self.notify(notify_topic)

    def set_master(self, info: MediaInfo | None) -> None:
        self.master = info
        self.master_audio_idx = 0
        self.master_audio_indices = (
            tuple(range(len(info.audio_streams))) if info and info.audio_streams else None
        )
        self.result = None
        self.offset_ms = 0.0
        self.tempo = 1.0
        self.use_segments = False
        self.segments = []
        self.notify("inputs")

    def set_master_track(self, idx: int) -> None:
        if not self.master or not self.master.audio_streams:
            return
        clamped = min(max(0, int(idx)), len(self.master.audio_streams) - 1)
        if self.master_audio_idx != clamped:
            self.master_audio_idx = clamped
            if self.batch_enabled:
                self.save_active_to_batch_entry()
            self.notify("inputs_track")

    def set_source(self, info: MediaInfo | None) -> None:
        self.source = info
        self.source_audio_idx = 0
        self.result = None
        self.offset_ms = 0.0
        self.tempo = 1.0
        self.use_segments = False
        self.segments = []
        self.track_configs = _default_track_configs(info, 0)
        self.notify("inputs")

    def set_source_track(self, idx: int) -> None:
        if not self.source or not self.source.audio_streams:
            return
        clamped = min(max(0, int(idx)), len(self.source.audio_streams) - 1)
        changed = self.source_audio_idx != clamped
        self.source_audio_idx = clamped
        if self.track_configs:
            for tc in self.track_configs:
                if tc.source == self.source:
                    tc.enabled = (tc.stream.position == clamped)
                    tc.default = (tc.stream.position == clamped)
        if self.batch_enabled:
            self.save_active_to_batch_entry()
        if changed:
            self.notify("inputs_track")
            self.notify("metadata")

    def add_source_tracks_from_media(self, info: MediaInfo) -> int:
        """Add all audio streams from an additional media file to track_configs."""
        count = 0
        if info and info.audio_streams:
            for s in info.audio_streams:
                self.track_configs.append(
                    TrackSelection(
                        stream=s,
                        enabled=True,
                        language=s.language or "spa",
                        title=s.title or info.path.stem,
                        default=False,
                        source=info,
                    )
                )
                count += 1
            if self.batch_enabled:
                self.save_active_to_batch_entry()
            self.notify("inputs_track")
            self.notify("metadata")
        return count

    def remove_track_config(self, tc: TrackSelection) -> None:
        if tc in self.track_configs:
            self.track_configs.remove(tc)
            if self.batch_enabled:
                self.save_active_to_batch_entry()
            self.notify("inputs_track")
            self.notify("metadata")

    def set_all_tracks_enabled(self, enabled: bool) -> None:
        for tc in self.track_configs:
            tc.enabled = enabled
        if enabled and self.track_configs:
            for tc in self.track_configs:
                if tc.source == self.source and tc.stream.position == self.source_audio_idx:
                    tc.default = True
        if self.batch_enabled:
            self.save_active_to_batch_entry()
        self.notify("metadata")

    def apply_result(self, res: SyncResult) -> None:
        self.result = res
        self.offset_ms = round(res.offset_ms, 1)
        self.tempo = res.tempo
        self.segments = list(res.segments)
        real_segs = [s for s in self.segments if not s.is_gap]
        self.use_segments = len(real_segs) > 1
        if self.batch_enabled and 0 <= self.active_batch_idx < len(self.batch_entries):
            self.save_active_to_batch_entry()
        self.notify("result")
        self.notify("calibration")

    # ── Batch management ──────────────────────────────────────────────

    def prepare_batch_entries(
        self, masters: list[MediaInfo], sources: list[MediaInfo]
    ) -> None:
        """Build or refresh ``batch_entries`` from probed master/source media lists."""
        existing_by_pair: dict[tuple[Path, Path], BatchEntry] = {
            (e.master_path, e.source_path): e for e in self.batch_entries
        }
        new_entries: list[BatchEntry] = []
        n = min(len(masters), len(sources))
        for i in range(n):
            m_info = masters[i]
            s_info = sources[i]
            key = (m_info.path, s_info.path)
            prev = existing_by_pair.get(key)
            if prev is not None:
                prev.master = m_info
                prev.source = s_info
                if not prev.track_configs:
                    prev.track_configs = _default_track_configs(s_info, prev.source_audio_idx)
                new_entries.append(prev)
            else:
                new_entries.append(
                    BatchEntry(
                        master_path=m_info.path,
                        source_path=s_info.path,
                        master=m_info,
                        source=s_info,
                        master_audio_idx=0,
                        source_audio_idx=0,
                        track_configs=_default_track_configs(s_info, 0),
                    )
                )
        self.batch_entries = new_entries
        if new_entries:
            idx = min(self.active_batch_idx, len(new_entries) - 1)
            self._load_batch_entry(idx)
            self.notify("inputs")
        self.notify("batch")

    def save_active_to_batch_entry(self) -> None:
        """Persist current active view parameters into ``batch_entries[active_batch_idx]``."""
        if not self.batch_entries or not (0 <= self.active_batch_idx < len(self.batch_entries)):
            return
        entry = self.batch_entries[self.active_batch_idx]
        if self.master and self.master.path == entry.master_path:
            entry.master = self.master
            entry.master_audio_idx = self.master_audio_idx
        if self.source and self.source.path == entry.source_path:
            entry.source = self.source
            entry.source_audio_idx = self.source_audio_idx
        entry.result = self.result
        entry.offset_ms = self.offset_ms
        entry.tempo = self.tempo
        entry.tempo_mode = self.tempo_mode
        entry.use_segments = self.use_segments
        entry.segments = list(self.segments)
        entry.track_configs = list(self.track_configs)
        entry.master_audio_indices = self.master_audio_indices

    def _load_batch_entry(self, idx: int) -> None:
        entry = self.batch_entries[idx]
        self.active_batch_idx = idx
        self.master = entry.master
        self.source = entry.source
        self.master_audio_idx = entry.master_audio_idx
        self.source_audio_idx = entry.source_audio_idx
        self.result = entry.result
        self.offset_ms = entry.offset_ms
        self.tempo = entry.tempo
        self.tempo_mode = entry.tempo_mode
        self.use_segments = entry.use_segments
        self.segments = list(entry.segments)
        self.track_configs = (
            list(entry.track_configs)
            if entry.track_configs
            else _default_track_configs(entry.source, entry.source_audio_idx)
        )
        self.master_audio_indices = entry.master_audio_indices

    def select_batch_entry(self, idx: int) -> None:
        """Switch active movie in batch mode to ``idx``."""
        if not self.batch_entries or not (0 <= idx < len(self.batch_entries)):
            return
        if idx == self.active_batch_idx and self.master == self.batch_entries[idx].master:
            return
        self.save_active_to_batch_entry()
        self._load_batch_entry(idx)
        self.notify("inputs")
        self.notify("result")
        self.notify("calibration")
        self.notify("metadata")
        self.notify("batch")

    def apply_batch_entry_result(self, idx: int, res: SyncResult) -> None:
        """Store analysis result for batch item ``idx``."""
        if not (0 <= idx < len(self.batch_entries)):
            return
        entry = self.batch_entries[idx]
        entry.result = res
        entry.offset_ms = round(res.offset_ms, 1)
        entry.tempo = res.tempo
        entry.segments = list(res.segments)
        real_segs = [s for s in entry.segments if not s.is_gap]
        entry.use_segments = len(real_segs) > 1
        entry.error = None
        if idx == self.active_batch_idx:
            self._load_batch_entry(idx)
            self.notify("result")
            self.notify("calibration")
        self.notify("batch")

