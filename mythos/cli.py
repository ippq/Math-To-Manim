"""The math-to-manim command line.

    math-to-manim run "explain the mechanism" --render --gif -q l
    math-to-manim run "the heat equation" --offline
    math-to-manim gif <run_id_or_mp4>       # standalone mp4 -> GIF conversion

Configuration comes from the environment (or a local, gitignored .env):
M2M_MODEL, M2M_COMMAND, M2M_TIMEOUT, M2M_RENDER_TIMEOUT, M2M_RUNS_DIR,
M2M_MANIM.
"""

from __future__ import annotations

import argparse
import sys

from mythos import __version__
from mythos.backends import DEFAULT_COMMAND, DEFAULT_MODEL, DEFAULT_TIMEOUT


def _cmd_run(args: argparse.Namespace) -> int:
    from mythos.harness import DEFAULT_RENDER_TIMEOUT, MythosHarness

    fallbacks = (tuple(m.strip() for m in args.fallbacks.split(",") if m.strip())
                 if args.fallbacks is not None else None)
    harness = MythosHarness(command=args.command, model=args.model,
                            timeout=args.timeout,
                            render_timeout=(args.render_timeout
                                            or DEFAULT_RENDER_TIMEOUT),
                            offline=args.offline,
                            model_fallbacks=fallbacks)
    manifest = harness.run(args.prompt, render=args.render, quality=args.quality,
                           max_repairs=args.max_repairs)
    if args.gif:
        renders = manifest.get("renders") or []
        rendered_ok = bool(renders) and renders[-1].get("exit_code") == 0
        if not rendered_ok:
            print("  [mythos] --gif skipped: no successful render to convert",
                  file=sys.stderr)
        elif manifest.get("medium") == "image":
            from mythos.gifs import find_scene_png
            png = find_scene_png(manifest["scene_name"])
            if png:
                print(f"  [mythos] image ready -> {png}")
            else:
                print("  [mythos] --gif skipped: medium is 'image' but no "
                      "PNG found", file=sys.stderr)
        else:
            from mythos.gifs import make_gif
            make_gif(manifest["run_id"], fps=args.gif_fps, width=args.gif_width)
    return 0


def _cmd_gif(args: argparse.Namespace) -> int:
    from mythos.gifs import make_gif

    make_gif(args.target, args.output, fps=args.fps, width=args.width)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="math-to-manim",
        description="Ask a question -> get one companion image or short GIF.",
    )
    parser.add_argument("--version", action="version",
                        version=f"math-to-manim {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run the visual-brief chain on a prompt")
    run.add_argument("prompt")
    run.add_argument("--render", action="store_true")
    run.add_argument("-q", "--quality", default="l", choices=list("lmhpk"))
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--command", default=DEFAULT_COMMAND)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run.add_argument("--render-timeout", type=float, default=None,
                     help="render wall-clock budget in seconds "
                          "(default: M2M_RENDER_TIMEOUT or 1800)")
    run.add_argument("--fallbacks", default=None,
                     help="comma-separated Anthropic fallback models via the "
                          "same CLI login (default: M2M_MODEL_FALLBACKS or "
                          "claude-opus-4-8,claude-sonnet-5; '' disables)")
    run.add_argument("--offline", action="store_true")
    run.add_argument("--max-repairs", type=int, default=3)
    run.add_argument("--gif", action="store_true",
                     help="after a successful --render, produce the final "
                          "embeddable asset automatically: a palette-"
                          "optimized GIF if medium was 'gif' (same recipe as "
                          "the `gif` subcommand), or just report the PNG "
                          "path if medium was 'image' (nothing to convert)")
    run.add_argument("--gif-fps", type=int, default=12)
    run.add_argument("--gif-width", type=int, default=640)
    run.set_defaults(func=_cmd_run)

    gif = sub.add_parser(
        "gif", help="Palette-optimized GIF from a run id or an .mp4")
    gif.add_argument("target", help="run id under runs/mythos/ or a path "
                                    "to a rendered .mp4")
    gif.add_argument("-o", "--output", default=None,
                     help="output path (default: next to the source)")
    gif.add_argument("--fps", type=int, default=12)
    gif.add_argument("--width", type=int, default=640)
    gif.set_defaults(func=_cmd_gif)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
