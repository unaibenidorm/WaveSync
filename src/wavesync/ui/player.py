"""A/B comparator player engine: GStreamer live dual-pipeline + 20 s FFmpeg preview clip fallback.

Architecture:

1. **Primary mode (Live GStreamer)**:
   * ``playbin`` A plays the master video into ``gtk4paintablesink`` (giving a
     ``Gdk.Paintable`` for ``Gtk.Picture``) and outputs the selected master
     audio stream A.
   * ``playbin`` B plays **only** the selected source audio track B. To
     guarantee that GStreamer never falls back to track #0 of a multi-audio
     container (and to make seeks sample-accurate and instant), WaveSync
     extracts the chosen B stream (``-map 0:<source_stream.index>``) to a
     dedicated temporary PCM WAV file in the background and loads it into
     ``playbin`` B, while also enforcing ``current-audio`` on ``audio-changed``
     signals.
   * Switching between A and B is instantaneous: one pipeline has
     ``mute=False, volume=1.0`` and the other ``mute=True, volume=0.0``.
2. **Fallback mode (20 s pre-rendered FFmpeg clip)**:
   * Renders a ~20 s MKV snippet in a temporary directory with video copied and
     two audio tracks (0 = Master A, 1 = processed Source B), regenerated with
     a 400 ms debounce on parameter changes.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_GST = True
except (ValueError, ImportError):
    Gst = None  # type: ignore[assignment]
    HAS_GST = False

from wavesync.core.errors import CancelToken  # noqa: E402
from wavesync.core.export import tempo_filters  # noqa: E402
from wavesync.core.ffmpeg import binaries, fmt_time, run  # noqa: E402
from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.models import AudioStream, MediaInfo, SyncSegment, TempoMode  # noqa: E402
from wavesync.core.tempo import a_to_b  # noqa: E402
from wavesync.ui.workers import BackgroundTask, run_in_background  # noqa: E402

# GstPlayFlags bitmasks
_GST_PLAY_FLAG_VIDEO = 1 << 0
_GST_PLAY_FLAG_AUDIO = 1 << 1
_GST_PLAY_FLAG_TEXT = 1 << 2


def extract_selected_track_wav(
    source: MediaInfo,
    source_stream: AudioStream,
    output_wav: Path,
    cancel: CancelToken | None = None,
) -> Path:
    """Extract exclusively ``source_stream`` to a stereo PCM WAV for instant, unambiguous playback."""
    ch = 2 if source_stream.channels >= 2 else 1
    sr = str(source_stream.sample_rate if source_stream.sample_rate > 0 else 48000)
    args = [
        str(binaries().ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source.path),
        "-map",
        f"0:{source_stream.index}",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        "aresample=async=1:first_pts=0",
        "-ac",
        str(ch),
        "-ar",
        sr,
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]
    run(args, cancel=cancel)
    return output_wav


def render_preview_clip(
    master: MediaInfo,
    source: MediaInfo,
    master_stream: AudioStream | None,
    source_stream: AudioStream,
    start_a: float,
    duration: float,
    offset: float,
    tempo: float,
    tempo_mode: TempoMode,
    output_path: Path,
    cancel: CancelToken | None = None,
) -> Path:
    """Render a short (~20 s) MKV clip with stream 0 = A audio and stream 1 = synced B audio."""
    start_a = max(0.0, min(start_a, max(0.0, master.duration - 2.0)))
    duration = max(2.0, min(duration, master.duration - start_a))

    # Corresponding position in B: t_B = (t_A - offset) * tempo
    t_b0 = a_to_b(start_a, offset, tempo)
    dur_b = duration * tempo + 2.0

    args = [
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
        "-ss",
        fmt_time(start_a),
        "-t",
        fmt_time(duration),
        "-i",
        str(master.path),
    ]
    b_lead_silence = 0.0
    if t_b0 >= 0.0:
        args += [
            "-err_detect",
            "ignore_err",
            "-fflags",
            "+genpts+discardcorrupt",
            "-ss",
            fmt_time(t_b0),
            "-t",
            fmt_time(dur_b),
            "-i",
            str(source.path),
        ]
    else:
        b_lead_silence = (-t_b0) / tempo
        args += [
            "-err_detect",
            "ignore_err",
            "-fflags",
            "+genpts+discardcorrupt",
            "-t",
            fmt_time(max(1.0, dur_b + t_b0)),
            "-i",
            str(source.path),
        ]

    chain = ["aresample=async=1", *tempo_filters(tempo, tempo_mode, source_stream.sample_rate)]
    chain.append("asetpts=PTS-STARTPTS")
    if b_lead_silence > 1e-4:
        chain.append(f"adelay={b_lead_silence * 1000.0:.3f}:all=1")
        chain.append("asetpts=PTS-STARTPTS")
    chain += [f"apad=whole_dur={fmt_time(duration)}", "asetpts=N/SR/TB"]
    fc = f"[1:{source_stream.index}]{','.join(chain)}[ab]"

    if master.has_video and master.video:
        args += ["-map", f"0:{master.video.index}"]
    if master_stream is not None:
        args += ["-map", f"0:{master_stream.index}"]
    args += [
        "-filter_complex",
        fc,
        "-map",
        "[ab]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]
    run(args, cancel=cancel)
    return output_path


class ABPlayerController:
    """Manages live GStreamer A/B playback and the 20 s pre-rendered clip fallback."""

    def __init__(
        self,
        on_state_update: Callable[[], None],
        on_status_message: Callable[[str], None],
    ) -> None:
        self._on_state_update = on_state_update
        self._on_status_message = on_status_message

        self._temp_dir = Path(tempfile.mkdtemp(prefix="wavesync-preview-"))
        self._clip_task: BackgroundTask | None = None
        self._wav_task: BackgroundTask | None = None
        self._prepared_b_key: tuple[str, int] | None = None
        self._using_extracted_b_wav: bool = False
        self._debounce_source_id: int = 0
        self._tick_source_id: int = 0

        self.active_branch: str = "A"  #: "A" or "B"
        self.playing: bool = False
        self.use_clip_fallback: bool = False
        self.clip_start_a: float = 0.0
        self.clip_duration: float = 20.0

        self._master: MediaInfo | None = None
        self._source: MediaInfo | None = None
        self._master_audio_idx: int = 0
        self._source_audio_idx: int = 0
        self._offset: float = 0.0
        self._tempo: float = 1.0
        self._tempo_mode: TempoMode = TempoMode.ATEMPO
        self._segments: list[SyncSegment] | None = None

        self.paintable: Gdk.Paintable | None = None
        self.source_paintable: Gdk.Paintable | None = None
        self.gst_available: bool = False
        self.side_by_side: bool = False
        self._playbin_a = None
        self._playbin_b = None
        self._playbin_source_video = None
        self._sink_a_volume = None
        self._sink_b_volume = None

        self._init_gstreamer()

    def _create_audio_sink_a(self):
        if not HAS_GST or Gst is None:
            return None
        sink = Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! volume name=volume ! audio/x-raw,channels=2 ! autoaudiosink",
            True,
        )
        if sink is not None:
            self._sink_a_volume = sink.get_by_name("volume")
        return sink

    def _create_audio_sink_b(self):
        if not HAS_GST or Gst is None:
            return None
        sink = Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! volume name=volume ! audio/x-raw,channels=2 ! autoaudiosink",
            True,
        )
        if sink is not None:
            self._sink_b_volume = sink.get_by_name("volume")
        return sink

    @staticmethod
    def _create_audio_filter_b():
        """Build an audio-filter bin: downmix to stereo then scaletempo.

        playbin's ``audio-filter`` property sits between the decoder and
        the audio-sink.  By downmixing to stereo *before* scaletempo we
        prevent the SIGSEGV that occurred when scaletempo received raw
        multichannel buffers from decoders like AC-3 5.1 (a52dec).
        """
        if not HAS_GST or Gst is None:
            return None
        try:
            filt = Gst.parse_bin_from_description(
                "audioconvert ! audioresample ! audio/x-raw,channels=2 ! scaletempo",
                True,
            )
            return filt
        except Exception:
            # scaletempo not available – try without it
            pass
        try:
            return Gst.parse_bin_from_description(
                "audioconvert ! audioresample ! audio/x-raw,channels=2",
                True,
            )
        except Exception:
            return None

    def _destroy_playbin_b(self) -> None:
        """Safely stop and tear down playbin_b and its bus signal watch."""
        if self._playbin_b is not None and Gst is not None:
            try:
                self._playbin_b.set_state(Gst.State.NULL)
                bus = self._playbin_b.get_bus()
                if bus is not None:
                    bus.remove_signal_watch()
            except Exception:
                pass
            self._playbin_b = None
            self._sink_b_volume = None

    def _recreate_playbin_b(self, uri: str | None = None) -> None:
        """Destroy old playbin_b and instantiate a fresh one.

        This completely circumvents the upstream GStreamer bug in gstscaletempo.c
        where gst_scaletempo_stop frees buf_queue without resetting bytes_queued,
        causing a SIGSEGV in memmove(NULL, ...) when a stopped playbin is restarted
        with a different URI or sample rate.
        """
        if not HAS_GST or Gst is None:
            return
        self._destroy_playbin_b()

        play_b = Gst.ElementFactory.make("playbin", "ws_play_b")
        if play_b is None:
            return

        sink_b = self._create_audio_sink_b()
        if sink_b is not None:
            play_b.set_property("audio-sink", sink_b)

        audio_filter_b = self._create_audio_filter_b()
        if audio_filter_b is not None:
            play_b.set_property("audio-filter", audio_filter_b)

        play_b.set_property("flags", _GST_PLAY_FLAG_AUDIO)
        play_b.connect("audio-changed", self._on_playbin_b_audio_changed)

        bus = play_b.get_bus()
        if bus is not None:
            bus.add_signal_watch()
            bus.connect("message::error", self._on_bus_error, "B")

        if uri:
            play_b.set_property("uri", uri)

        self._playbin_b = play_b

    def _init_gstreamer(self) -> None:
        if not HAS_GST or Gst is None:
            return
        sink = Gst.ElementFactory.make("gtk4paintablesink", "ws_vsink")
        play_a = Gst.ElementFactory.make("playbin", "ws_play_a")
        sink_source = Gst.ElementFactory.make("gtk4paintablesink", "ws_source_vsink")
        play_source_video = Gst.ElementFactory.make("playbin", "ws_play_source_video")
        if sink is None or play_a is None or sink_source is None or play_source_video is None:
            return

        self.paintable = sink.get_property("paintable")
        play_a.set_property("video-sink", sink)
        self.source_paintable = sink_source.get_property("paintable")
        play_source_video.set_property("video-sink", sink_source)
        play_source_video.set_property("flags", _GST_PLAY_FLAG_VIDEO)

        sink_a = self._create_audio_sink_a()
        if sink_a is not None:
            play_a.set_property("audio-sink", sink_a)

        flags_a = play_a.get_property("flags")
        play_a.set_property(
            "flags", (flags_a | _GST_PLAY_FLAG_VIDEO | _GST_PLAY_FLAG_AUDIO) & ~_GST_PLAY_FLAG_TEXT
        )
        play_a.connect("audio-changed", self._on_playbin_a_audio_changed)
        bus_a = play_a.get_bus()
        if bus_a is not None:
            bus_a.add_signal_watch()
            bus_a.connect("message::error", self._on_bus_error, "A")

        self._playbin_a = play_a
        self._playbin_source_video = play_source_video
        self._recreate_playbin_b()
        self.gst_available = True
        self._tick_source_id = GLib.timeout_add(200, self._on_tick)

    def _on_bus_error(self, _bus, msg, name: str) -> None:
        err, _dbg = msg.parse_error()
        msg_str = (err.message or "").lower()
        # Suppress transient GStreamer 'internal data stream error' (emitted during seeks,
        # preroll or flushes before extracted WAV is swapped in)
        if (
            "data stream" in msg_str
            or "flujo de datos" in msg_str
            or (
                HAS_GST
                and Gst is not None
                and hasattr(Gst, "StreamError")
                and err.matches(Gst.StreamError.quark(), Gst.StreamError.FAILED)
            )
        ):
            return
        if name == "B" and not self.use_clip_fallback:
            self._on_status_message(
                _("Aviso en reproducción de audio B: {err}").format(err=err.message)
            )

    def _on_playbin_a_audio_changed(self, _playbin) -> None:
        GLib.idle_add(self._enforce_audio_streams)

    def _on_playbin_b_audio_changed(self, _playbin) -> None:
        GLib.idle_add(self._enforce_audio_streams)

    def _enforce_audio_streams(self) -> bool:
        if not self.gst_available:
            return GLib.SOURCE_REMOVE
        if self._playbin_a is not None:
            n_a = int(self._playbin_a.get_property("n-audio") or 0)
            if self.use_clip_fallback:
                desired_a = 1 if (self.active_branch == "B" and n_a > 1) else 0
            else:
                desired_a = min(self._master_audio_idx, max(0, n_a - 1))
            if n_a > 0 and int(self._playbin_a.get_property("current-audio")) != desired_a:
                self._playbin_a.set_property("current-audio", desired_a)

        if not self.use_clip_fallback and self._playbin_b is not None:
            n_b = int(self._playbin_b.get_property("n-audio") or 0)
            desired_b = 0 if self._using_extracted_b_wav else min(self._source_audio_idx, max(0, n_b - 1))
            if n_b > 0 and int(self._playbin_b.get_property("current-audio")) != desired_b:
                self._playbin_b.set_property("current-audio", desired_b)
        return GLib.SOURCE_REMOVE

    def _set_sink_vol(self, sink_elem, mute: bool, vol: float) -> None:
        if sink_elem is not None:
            try:
                sink_elem.set_property("mute", mute)
                sink_elem.set_property("volume", vol)
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop pipelines and remove temporary preview files."""
        if self._tick_source_id:
            GLib.source_remove(self._tick_source_id)
            self._tick_source_id = 0
        if self._debounce_source_id:
            GLib.source_remove(self._debounce_source_id)
            self._debounce_source_id = 0
        if self._clip_task:
            self._clip_task.cancel()
        if self._wav_task:
            self._wav_task.cancel()
        if self._playbin_a is not None and Gst is not None:
            try:
                self._playbin_a.set_state(Gst.State.NULL)
            except Exception:
                pass
        self._destroy_playbin_b()
        if self._playbin_source_video is not None and Gst is not None:
            try:
                self._playbin_source_video.set_state(Gst.State.NULL)
            except Exception:
                pass
        self._sink_a_volume = None
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def load_media(
        self,
        master: MediaInfo | None,
        source: MediaInfo | None,
        master_audio_idx: int,
        source_audio_idx: int,
    ) -> None:
        same_files = (
            self._master is not None
            and master is not None
            and self._master.path == master.path
            and self._source is not None
            and source is not None
            and self._source.path == source.path
        )
        self._master = master
        self._source = source
        self._master_audio_idx = master_audio_idx
        self._source_audio_idx = source_audio_idx
        if not self.gst_available or master is None or source is None:
            return

        if same_files and not self.use_clip_fallback:
            # If changing to a track whose WAV is not extracted yet, immediately switch playbin_b
            # back to playing source.path directly so the user hears sound right away
            if self._source and self._source.audio_streams:
                idx = min(max(0, self._source_audio_idx), len(self._source.audio_streams) - 1)
                s_stream = self._source.audio_streams[idx]
                key = (str(self._source.path.resolve()), s_stream.index)
                if self._prepared_b_key != key:
                    self._using_extracted_b_wav = False
                    self._recreate_playbin_b(self._source.path.resolve().as_uri())
                    if self._playbin_b is not None:
                        self._playbin_b.set_state(Gst.State.PLAYING if self.playing else Gst.State.PAUSED)
                    self._apply_branch_mute()
            self._enforce_audio_streams()
            self._prepare_b_wav_if_needed()
            if self.use_clip_fallback:
                self.regenerate_preview_clip()
            return

        self.pause()
        self._using_extracted_b_wav = False
        if self._playbin_a is not None:
            self._playbin_a.set_state(Gst.State.NULL)
        self._recreate_playbin_b(source.path.resolve().as_uri())
        if self._playbin_source_video is not None:
            self._playbin_source_video.set_state(Gst.State.NULL)
        if self._playbin_a is not None:
            self._playbin_a.set_property("uri", master.path.resolve().as_uri())
        if self._playbin_source_video is not None and self.side_by_side and source.has_video:
            self._playbin_source_video.set_property("uri", source.path.resolve().as_uri())

        if self._playbin_a is not None:
            self._playbin_a.set_state(Gst.State.PAUSED)
        if self._playbin_b is not None:
            self._playbin_b.set_state(Gst.State.PAUSED)
        if self._playbin_source_video is not None and self.side_by_side and source.has_video:
            self._playbin_source_video.set_state(Gst.State.PAUSED)
        self._apply_branch_mute()
        self._enforce_audio_streams()
        self._prepare_b_wav_if_needed()

    def set_side_by_side(self, enabled: bool) -> None:
        """Enable or disable dual side-by-side video decoding."""
        self.side_by_side = enabled
        if not self.gst_available or Gst is None or self._playbin_source_video is None:
            return
        if enabled:
            if self._source and self._source.has_video:
                self._playbin_source_video.set_property("uri", self._source.path.resolve().as_uri())
                state = Gst.State.PLAYING if self.playing else Gst.State.PAUSED
                self._playbin_source_video.set_state(state)
                self._sync_source_video_to_a(self.position())
        else:
            self._playbin_source_video.set_state(Gst.State.NULL)

    def _prepare_b_wav_if_needed(self) -> None:
        """Extract the selected B audio stream into a dedicated WAV so track selection cannot fail."""
        if self._source is None or not self._source.audio_streams:
            return
        idx = min(max(0, self._source_audio_idx), len(self._source.audio_streams) - 1)
        s_stream = self._source.audio_streams[idx]
        src_resolved = str(self._source.path.resolve())
        key = (src_resolved, s_stream.index)
        safe_hash = hashlib.sha1(src_resolved.encode("utf-8")).hexdigest()[:10]
        wav_path = self._temp_dir / f"b_{safe_hash}_stream_{s_stream.index}.wav"

        if wav_path.is_file():
            self._prepared_b_key = key
            self._switch_playbin_b_to_wav(wav_path)
            return

        if self._wav_task is not None:
            self._wav_task.cancel()

        source = self._source
        self._using_extracted_b_wav = False

        def work(cancel: CancelToken, _prog) -> Path:
            return extract_selected_track_wav(source, s_stream, wav_path, cancel=cancel)

        def done(path: Path) -> None:
            if self._source is None:
                return
            current_src = str(self._source.path.resolve())
            idx = min(max(0, self._source_audio_idx), len(self._source.audio_streams) - 1)
            current_stream = self._source.audio_streams[idx]
            if (current_src, current_stream.index) != key:
                return
            self._prepared_b_key = key
            self._switch_playbin_b_to_wav(path)

        def failed(_error) -> None:
            self._on_status_message(
                _("No se pudo preparar la pista B; se usará la reproducción directa.")
            )

        self._wav_task = run_in_background(work, on_done=done, on_error=failed)

    def _switch_playbin_b_to_wav(self, wav_path: Path) -> None:
        if not self.gst_available or Gst is None or self.use_clip_fallback:
            return
        was_playing = self.playing
        cur_pos_a = self.position()
        self._using_extracted_b_wav = True
        self._recreate_playbin_b(wav_path.resolve().as_uri())
        if self._playbin_b is not None:
            self._playbin_b.set_state(Gst.State.PLAYING if was_playing else Gst.State.PAUSED)
        self._apply_branch_mute()
        GLib.timeout_add(120, self._deferred_sync_b, cur_pos_a)

    def _deferred_sync_b(self, target_a: float) -> bool:
        self._sync_b_to_a(self.position() if self.playing else target_a)
        return GLib.SOURCE_REMOVE

    def _effective_offset(self, t_a: float) -> float:
        """Return the segment offset covering ``t_a`` when multi-segment sync is active."""
        if self._segments:
            for seg in self._segments:
                if not seg.is_gap and seg.start <= t_a <= seg.end:
                    return seg.offset if seg.offset is not None else self._offset
            real = [s for s in self._segments if not s.is_gap and s.offset is not None]
            if real:
                if t_a < real[0].start:
                    return real[0].offset  # type: ignore[return-value]
                return real[-1].offset  # type: ignore[return-value]
        return self._offset

    def update_sync_params(
        self,
        offset_sec: float,
        tempo: float,
        tempo_mode: TempoMode,
        *,
        segments: list[SyncSegment] | None = None,
        immediate: bool = False,
    ) -> None:
        def _seg_repr(s: SyncSegment) -> tuple[float, float, float | None]:
            off = round(s.offset, 5) if s.offset is not None else None
            return (round(s.start, 3), round(s.end, 3), off)

        seg_key_old = [_seg_repr(s) for s in (self._segments or [])]
        seg_key_new = [_seg_repr(s) for s in (segments or [])]
        changed = (
            abs(self._offset - offset_sec) > 1e-5
            or abs(self._tempo - tempo) > 1e-7
            or self._tempo_mode != tempo_mode
            or seg_key_old != seg_key_new
        )
        self._offset = offset_sec
        self._tempo = tempo
        self._tempo_mode = tempo_mode
        self._segments = list(segments) if segments else None
        if not changed and not immediate:
            return
        if self._debounce_source_id:
            GLib.source_remove(self._debounce_source_id)
            self._debounce_source_id = 0
        delay_ms = 0 if immediate else (400 if self.use_clip_fallback else 120)
        if delay_ms == 0:
            self._apply_sync_now()
        else:
            self._debounce_source_id = GLib.timeout_add(delay_ms, self._on_debounce_fired)

    def _on_debounce_fired(self) -> bool:
        self._debounce_source_id = 0
        self._apply_sync_now()
        return GLib.SOURCE_REMOVE

    def _apply_sync_now(self) -> None:
        if self.use_clip_fallback:
            self.regenerate_preview_clip()
        else:
            self._sync_b_to_a(self.position())

    def set_branch(self, branch: str) -> None:
        self.active_branch = "B" if branch.upper() == "B" else "A"
        self._apply_branch_mute()
        self._on_state_update()

    def toggle_branch(self) -> str:
        self.set_branch("B" if self.active_branch == "A" else "A")
        return self.active_branch

    def _apply_branch_mute(self) -> None:
        if not self.gst_available:
            return
        if self.use_clip_fallback:
            # In single-file preview clip mode, audio 0 = A and audio 1 = B
            if self._playbin_a is not None:
                self._playbin_a.set_property("mute", False)
                self._playbin_a.set_property("volume", 1.0)
            self._set_sink_vol(self._sink_a_volume, False, 1.0)
            if self._playbin_b is not None:
                self._playbin_b.set_property("mute", True)
                self._playbin_b.set_property("volume", 0.0)
            self._set_sink_vol(self._sink_b_volume, True, 0.0)
            self._enforce_audio_streams()
        else:
            is_b = self.active_branch == "B"
            if self._playbin_a is not None:
                self._playbin_a.set_property("mute", is_b)
                self._playbin_a.set_property("volume", 0.0 if is_b else 1.0)
            self._set_sink_vol(self._sink_a_volume, is_b, 0.0 if is_b else 1.0)

            pos_a = self.position()
            eff_offset = self._effective_offset(pos_a)
            t_b = a_to_b(pos_a, eff_offset, self._tempo)
            b_in_silence = is_b and (t_b < 0.0)

            mute_b = (not is_b) or b_in_silence
            vol_b = 0.0 if mute_b else 1.0
            if self._playbin_b is not None:
                self._playbin_b.set_property("mute", mute_b)
                self._playbin_b.set_property("volume", vol_b)
            self._set_sink_vol(self._sink_b_volume, mute_b, vol_b)

            if is_b and self.playing and self._playbin_b is not None:
                self._playbin_b.set_state(Gst.State.PLAYING)

            self._enforce_audio_streams()
            if is_b:
                self._sync_b_to_a(pos_a)

    def play(self) -> None:
        if not self.gst_available or self._master is None:
            return
        if self._playbin_a is not None:
            self._playbin_a.set_state(Gst.State.PLAYING)
        if not self.use_clip_fallback and self._source is not None:
            if self._playbin_b is not None:
                self._playbin_b.set_state(Gst.State.PLAYING)
            if self.side_by_side and self._playbin_source_video is not None and self._source.has_video:
                self._playbin_source_video.set_state(Gst.State.PLAYING)
        self.playing = True
        self._apply_branch_mute()
        self._on_state_update()

    def pause(self) -> None:
        self.playing = False
        if not self.gst_available or Gst is None:
            return
        if self._playbin_a is not None:
            self._playbin_a.set_state(Gst.State.PAUSED)
        if self._playbin_b is not None:
            self._playbin_b.set_state(Gst.State.PAUSED)
        if self._playbin_source_video is not None:
            self._playbin_source_video.set_state(Gst.State.PAUSED)
        self._on_state_update()

    def toggle_play(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def duration(self) -> float:
        if self._master is None:
            return 0.0
        return self._master.duration

    def position(self) -> float:
        if not self.gst_available or Gst is None or self._playbin_a is None:
            return 0.0
        ok, pos = self._playbin_a.query_position(Gst.Format.TIME)
        if not ok:
            return self.clip_start_a if self.use_clip_fallback else 0.0
        sec = float(pos) / float(Gst.SECOND)
        return (self.clip_start_a + sec) if self.use_clip_fallback else sec

    def _position_b(self) -> float | None:
        if not self.gst_available or Gst is None or self._playbin_b is None:
            return None
        ok, pos = self._playbin_b.query_position(Gst.Format.TIME)
        if not ok:
            return None
        return float(pos) / float(Gst.SECOND)

    def seek(self, target_a_sec: float) -> None:
        if not self.gst_available or Gst is None or self._master is None or self._playbin_a is None:
            return
        target = max(0.0, min(float(target_a_sec), self._master.duration))
        if self.use_clip_fallback:
            if not (self.clip_start_a <= target <= self.clip_start_a + self.clip_duration):
                self.clip_start_a = max(0.0, target - 2.0)
                self.regenerate_preview_clip()
                return
            rel = max(0.0, target - self.clip_start_a)
            self._playbin_a.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(rel * Gst.SECOND),
            )
        else:
            self._playbin_a.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                int(target * Gst.SECOND),
            )
            self._sync_b_to_a(target)
            if self.side_by_side:
                self._sync_source_video_to_a(target)
        self._on_state_update()

    def _sync_b_to_a(self, t_a: float) -> None:
        if (
            not self.gst_available
            or Gst is None
            or self._source is None
            or self._playbin_b is None
            or self.use_clip_fallback
        ):
            return
        eff_offset = self._effective_offset(t_a)
        t_b = a_to_b(t_a, eff_offset, self._tempo)
        if t_b < 0.0:
            if self.active_branch == "B":
                self._playbin_b.set_property("mute", True)
                self._playbin_b.set_property("volume", 0.0)
                self._set_sink_vol(self._sink_b_volume, True, 0.0)
            t_b_clamped = 0.0
        else:
            if self.active_branch == "B":
                self._playbin_b.set_property("mute", False)
                self._playbin_b.set_property("volume", 1.0)
                self._set_sink_vol(self._sink_b_volume, False, 1.0)
            t_b_clamped = max(0.0, min(t_b, max(0.0, self._source.duration - 0.05)))

        rate = max(0.5, min(2.0, float(self._tempo)))
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        self._playbin_b.seek(
            rate,
            Gst.Format.TIME,
            flags,
            Gst.SeekType.SET,
            int(t_b_clamped * Gst.SECOND),
            Gst.SeekType.NONE,
            0,
        )

    def _sync_source_video_to_a(self, t_a: float) -> None:
        if (
            not self.side_by_side
            or self._playbin_source_video is None
            or not self._source
            or not self._source.has_video
            or Gst is None
        ):
            return
        eff_offset = self._effective_offset(t_a)
        t_b = a_to_b(t_a, eff_offset, self._tempo)
        t_b = max(0.0, min(t_b, max(0.0, self._source.duration - 0.05)))
        self._playbin_source_video.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            int(t_b * Gst.SECOND),
        )

    def regenerate_preview_clip(self, on_ready: Callable[[Path], None] | None = None) -> None:
        """Render a 20 s preview clip in the background (fallback mode)."""
        if self._master is None or self._source is None or not self._source.audio_streams:
            return
        if self._clip_task is not None:
            self._clip_task.cancel()

        master = self._master
        source = self._source
        m_stream = (
            master.audio_streams[min(self._master_audio_idx, len(master.audio_streams) - 1)]
            if master.audio_streams
            else None
        )
        s_stream = source.audio_streams[min(self._source_audio_idx, len(source.audio_streams) - 1)]
        start_a = self.clip_start_a
        dur = self.clip_duration
        offset = self._effective_offset(start_a + dur * 0.5)
        tempo = self._tempo
        tempo_mode = self._tempo_mode
        clip_path = self._temp_dir / "preview_ab.mkv"

        self._on_status_message(
            _("Generando clip de 20 s con pista B ({desc})…").format(desc=s_stream.describe())
        )

        def work(cancel: CancelToken, _prog) -> Path:
            return render_preview_clip(
                master,
                source,
                m_stream,
                s_stream,
                start_a,
                dur,
                offset,
                tempo,
                tempo_mode,
                clip_path,
                cancel=cancel,
            )

        def done(path: Path) -> None:
            self._on_status_message(_("Clip de previsualización listo."))
            if self.gst_available and self.use_clip_fallback and Gst is not None:
                if self._master is None or self._master.path != master.path:
                    return
                was_playing = self.playing
                self._playbin_a.set_state(Gst.State.NULL)
                self._destroy_playbin_b()
                self._playbin_a.set_property("uri", path.resolve().as_uri())
                self._playbin_a.set_state(Gst.State.PLAYING if was_playing else Gst.State.PAUSED)
                self._apply_branch_mute()
                GLib.timeout_add(80, self._enforce_audio_streams)
            if on_ready is not None:
                on_ready(path)

        self._clip_task = run_in_background(work, on_done=done)

    def _on_tick(self) -> bool:
        if not self.playing:
            return GLib.SOURCE_CONTINUE
        try:
            if not self.use_clip_fallback and self.active_branch == "B" and self._playbin_b is not None:
                pos_a = self.position()
                eff_offset = self._effective_offset(pos_a)
                expected_b = a_to_b(pos_a, eff_offset, self._tempo)
                if expected_b < 0.0:
                    # In pre-offset silence: ensure B is muted
                    self._playbin_b.set_property("mute", True)
                    self._playbin_b.set_property("volume", 0.0)
                    self._set_sink_vol(self._sink_b_volume, True, 0.0)
                else:
                    # In audible region: ensure B is unmuted
                    self._playbin_b.set_property("mute", False)
                    self._playbin_b.set_property("volume", 1.0)
                    self._set_sink_vol(self._sink_b_volume, False, 1.0)

                    pos_b = self._position_b()
                    if pos_b is not None and 0.0 <= expected_b <= (self._source.duration if self._source else 0.0):
                        # Use a 120 ms tolerance window so ordinary demuxer / clock packet jitter
                        # does not cause continuous flush-seeks (which would mute the audio output)
                        if abs(pos_b - expected_b) > 0.12:
                            self._sync_b_to_a(pos_a)
            self._on_state_update()
        except Exception:
            pass
        return GLib.SOURCE_CONTINUE
