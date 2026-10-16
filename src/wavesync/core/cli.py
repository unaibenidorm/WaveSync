"""Command-line interface for :mod:`wavesync.core` (headless validation & scripting).

Examples::

    # Inspect streams of a file
    python -m wavesync.core.cli probe pelicula_4k.mkv

    # Synchronise file B against master A
    python -m wavesync.core.cli analyze pelicula_4k.mkv doblaje_480p.avi

    # Synchronise and immediately export to MKV
    python -m wavesync.core.cli sync pelicula_4k.mkv doblaje_480p.avi -o salida.mkv --lang spa --title "Castellano"

    # Keep only selected master audio tracks and add external subtitles
    python -m wavesync.core.cli sync master.mkv doblaje.mkv -o salida.mkv \
        --master-audio-indices 0,2 --subtitle subtitulos.srt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wavesync.core.batch import BatchItem, BatchOptions, process_batch
from wavesync.core.errors import WaveSyncError
from wavesync.core.export import export
from wavesync.core.models import Container, ExportJob, ExportTrack, TempoMode
from wavesync.core.probe import probe, require_audio
from wavesync.core.sync import SyncParams, analyze


def _print_info(label: str, info_path: str) -> None:
    info = probe(info_path)
    print(f"=== {label}: {info.path.name} ===")
    print(f"  Formato  : {info.format_name} | Duración: {info.duration:.2f} s")
    if info.video:
        v = info.video
        hdr = " [HDR]" if v.hdr else ""
        print(
            f"  Vídeo    : #{v.index} {v.codec.upper()} {v.width}x{v.height} "
            f"fps={v.fps_real:.3f} (avg {v.fps_avg:.3f}){hdr}"
        )
    else:
        print("  Vídeo    : (sin vídeo)")
    if info.audio_streams:
        for a in info.audio_streams:
            d = " [default]" if a.default else ""
            print(f"  Audio {a.position}: stream #{a.index} · {a.describe()} · {a.sample_rate} Hz{d}")
    else:
        print("  Audio    : (ninguno)")


def _cmd_probe(args: argparse.Namespace) -> int:
    for p in args.files:
        _print_info("Archivo", p)
    return 0


def _progress(frac: float, msg: str) -> None:
    sys.stderr.write(f"\r[{frac * 100:5.1f}%] {msg:<60}")
    sys.stderr.flush()
    if frac >= 1.0:
        sys.stderr.write("\n")


def _run_analysis(args: argparse.Namespace):
    info_a = probe(args.master)
    info_b = probe(args.source)
    require_audio(info_b)
    if args.source_track >= len(info_b.audio_streams):
        raise SystemExit(
            f"Índice de pista B inválido ({args.source_track}); hay {len(info_b.audio_streams)} pista(s)."
        )
    a_stream = None
    if info_a.audio_streams:
        idx = min(args.master_track, len(info_a.audio_streams) - 1)
        a_stream = info_a.audio_streams[idx]
    b_stream = info_b.audio_streams[args.source_track]
    params = SyncParams(
        search_range=args.search_range,
        n_windows=args.windows,
        audio_refine=not args.no_audio_refine,
        audio_mode=args.audio_mode,
        audio_method=args.audio_method,
    )
    res = analyze(info_a, info_b, a_stream, b_stream, params=params, on_progress=_progress)
    print("\n=== Resultado de sincronización ===")
    print(f"  Veredicto       : {res.verdict.label} (modo primario: {res.mode})")
    print(f"  Confianza global: {res.confidence:.1%}")
    print(f"  Offset principal: {res.offset_ms:+.1f} ms ({res.offset:+.6f} s)")
    preset_txt = f" [preset {res.tempo_preset}]" if res.tempo_preset else ""
    print(f"  Factor de tempo : {res.tempo:.6f} (medido: {res.tempo_measured:.6f}){preset_txt}")
    print(f"  Refinado audio  : {'sí' if res.audio_refined else 'no'}")
    print(f"  Tramos ({len(res.segments)}):")
    for i, s in enumerate(res.segments, 1):
        if s.is_gap:
            print(f"    {i}. [{s.start:7.2f}s – {s.end:7.2f}s]  HUECO (silencio)")
        else:
            print(
                f"    {i}. [{s.start:7.2f}s – {s.end:7.2f}s]  offset={s.offset_ms:+8.1f} ms  "
                f"conf={s.confidence:.0%} ({s.n_windows} ventanas)"
            )
    if res.messages:
        print("  Notas:")
        for m in res.messages:
            print(f"    - {m}")
    return info_a, info_b, b_stream, res


def _cmd_analyze(args: argparse.Namespace) -> int:
    _run_analysis(args)
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    subtitle_paths = tuple(Path(path) for path in args.subtitle_paths)
    missing_subtitles = [path for path in subtitle_paths if not path.is_file()]
    if missing_subtitles:
        formatted = ", ".join(str(path) for path in missing_subtitles)
        raise SystemExit(f"Archivo de subtítulos no encontrado o no es un archivo: {formatted}")

    info_a, info_b, b_stream, res = _run_analysis(args)
    if args.master_audio_indices is not None:
        invalid = [
            index
            for index in args.master_audio_indices
            if not 0 <= index < len(info_a.audio_streams)
        ]
        if invalid:
            raise SystemExit(
                "Índice(s) de audio del máster inválido(s): "
                f"{', '.join(str(index) for index in invalid)}; "
                f"hay {len(info_a.audio_streams)} pista(s) de audio."
            )

    out = Path(args.output)
    container = Container.MP4 if out.suffix.lower() == ".mp4" else Container.MKV
    job = ExportJob(
        master=info_a,
        source=info_b,
        output=out,
        tracks=[ExportTrack(stream=b_stream, language=args.lang, title=args.title, default=args.default)],
        offset=res.offset,
        tempo=res.tempo,
        tempo_mode=TempoMode(args.tempo_mode),
        segments=res.segments if len(res.segments) > 1 else None,
        codec=args.codec,
        bitrate_kbps=args.bitrate,
        container=container,
        keep_original_audio=not args.drop_master_audio,
        master_audio_indices=args.master_audio_indices,
        subtitle_paths=subtitle_paths,
        overwrite=args.overwrite,
    )
    report = export(job, on_progress=_progress)
    print(f"Exportado y verificado: {report.info.path} ({len(report.info.audio_streams)} pistas de audio)")
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    try:
        entries = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"No se pudo leer el manifiesto de lote: {exc}") from exc
    if not isinstance(entries, list):
        raise SystemExit("El manifiesto de lote debe ser una lista JSON.")

    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    items: list[BatchItem] = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or "master" not in entry or "source" not in entry:
            raise SystemExit(f"Entrada {index}: se requieren 'master' y 'source'.")
        master = Path(entry["master"])
        source = Path(entry["source"])
        output_value = entry.get("output")
        if output_value:
            output = Path(output_value)
        elif output_dir:
            output = output_dir / f"{master.stem}.wavesync.mkv"
        else:
            output = master.with_name(f"{master.stem}.wavesync.mkv")
        items.append(
            BatchItem(
                master=master,
                source=source,
                output=output,
                master_track=int(entry.get("master_track", 0)),
                source_track=int(entry.get("source_track", 0)),
                language=str(entry.get("lang", args.lang)),
                title=str(entry.get("title", args.title)),
            )
        )

    options = BatchOptions(
        params=SyncParams(
            search_range=args.search_range,
            n_windows=args.windows,
            audio_refine=not args.no_audio_refine,
            audio_mode=args.audio_mode,
            audio_method=args.audio_method,
        ),
        codec=args.codec,
        bitrate_kbps=args.bitrate,
        tempo_mode=TempoMode(args.tempo_mode),
        keep_original_audio=not args.drop_master_audio,
        overwrite=args.overwrite,
    )
    results = process_batch(items, options, on_progress=_progress)
    failures = [result for result in results if result.error]
    print(f"\nLote terminado: {len(results) - len(failures)}/{len(results)} exportaciones correctas.")
    for result in failures:
        print(f"  ERROR {result.item.master.name}: {result.error}", file=sys.stderr)
    return 1 if failures else 0


def _parse_index_list(value: str) -> tuple[int, ...]:
    """Parse comma-separated, zero-based stream positions for argparse."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError("use una lista como 0,2,3")
    try:
        indices = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("los índices deben ser enteros, por ejemplo 0,2") from exc
    if any(index < 0 for index in indices):
        raise argparse.ArgumentTypeError("los índices deben ser 0 o mayores")
    if len(set(indices)) != len(indices):
        raise argparse.ArgumentTypeError("no se permiten índices de audio repetidos")
    return indices


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wavesync-cli", description="WaveSync core CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="Inspeccionar pistas multimedia con ffprobe")
    p_probe.add_argument("files", nargs="+")
    p_probe.set_defaults(func=_cmd_probe)

    for name, fn, need_out in (("analyze", _cmd_analyze, False), ("sync", _cmd_sync, True)):
        p = sub.add_parser(name, help="Analizar sincronización entre A y B" + (" y exportar" if need_out else ""))
        p.add_argument("master", help="Archivo A (máster de alta calidad)")
        p.add_argument("source", help="Archivo B (fuente de audio)")
        p.add_argument("--master-track", type=int, default=0, help="Índice 0-based de pista de audio en A")
        p.add_argument("--source-track", type=int, default=0, help="Índice 0-based de pista de audio en B")
        p.add_argument("--search-range", type=float, default=30.0, help="Rango de búsqueda inicial (± segundos)")
        p.add_argument("--windows", type=int, default=24, help="Número de ventanas (≥20)")
        p.add_argument("--no-audio-refine", action="store_true", help="Desactivar refinamiento por audio")
        p.add_argument("--audio-mode", choices=("full", "sides"), default="full")
        p.add_argument("--audio-method", choices=("gcc", "envelope"), default="gcc")
        if need_out:
            p.add_argument("-o", "--output", required=True, help="Archivo de salida (.mkv / .mp4)")
            p.add_argument("--lang", default="und", help="Código de idioma ISO 639-2")
            p.add_argument("--title", default="", help="Título de la nueva pista")
            p.add_argument("--default", action="store_true", help="Marcar la nueva pista como predeterminada")
            p.add_argument("--codec", choices=("aac", "ac3", "eac3", "flac", "opus", "copy"), default="aac")
            p.add_argument("--bitrate", type=int, default=None, help="Bitrate en kbps")
            p.add_argument("--tempo-mode", choices=("atempo", "asetrate"), default="atempo")
            p.add_argument(
                "--drop-master-audio",
                action="store_true",
                help="No conservar ninguna pista de audio original del máster",
            )
            p.add_argument(
                "--master-audio-indices",
                type=_parse_index_list,
                default=None,
                metavar="N[,N...]",
                help=(
                    "Conservar solo estas posiciones 0-based de audio del máster "
                    "(separadas por comas; sustituye a --drop-master-audio)"
                ),
            )
            p.add_argument(
                "--subtitle",
                "--subtitle-path",
                "--subtitle-paths",
                dest="subtitle_paths",
                action="append",
                default=[],
                metavar="PATH",
                help="Añadir un archivo de subtítulos externo (se puede repetir)",
            )
            p.add_argument("-y", "--overwrite", action="store_true")
        p.set_defaults(func=fn)

    p_batch = sub.add_parser("batch", help="Sincronizar y exportar múltiples parejas desde un manifiesto JSON")
    p_batch.add_argument("manifest", help="Lista JSON de parejas master/source y pistas seleccionadas")
    p_batch.add_argument("--output-dir", help="Carpeta de salida si una entrada no define 'output'")
    p_batch.add_argument("--lang", default="und", help="Idioma predeterminado de la pista nueva")
    p_batch.add_argument("--title", default="", help="Título predeterminado de la pista nueva")
    p_batch.add_argument("--search-range", type=float, default=30.0)
    p_batch.add_argument("--windows", type=int, default=24)
    p_batch.add_argument("--no-audio-refine", action="store_true")
    p_batch.add_argument("--audio-mode", choices=("full", "sides"), default="full")
    p_batch.add_argument("--audio-method", choices=("gcc", "envelope"), default="gcc")
    p_batch.add_argument("--codec", choices=("aac", "ac3", "eac3", "flac", "opus", "copy"), default="aac")
    p_batch.add_argument("--bitrate", type=int, default=None)
    p_batch.add_argument("--tempo-mode", choices=("atempo", "asetrate"), default="atempo")
    p_batch.add_argument("--drop-master-audio", action="store_true")
    p_batch.add_argument("-y", "--overwrite", action="store_true")
    p_batch.set_defaults(func=_cmd_batch)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except WaveSyncError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
