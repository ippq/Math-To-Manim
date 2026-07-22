"""Mythos harness: a minimal atom-illustration chain, driven through Claude Fable 5.

The agent charter lives in ``mythos/agents/mythos-visual-brief.md`` (mirrored
to ``.claude/agents/`` for native Claude Code use). This harness reads that
charter and drives it headlessly through the configured model backend, so
interactive sessions and automated runs share one source of truth.

Chain:
    visual-brief -> [codegen -> verify -> render]

One reasoning stage decides medium ("image" or "gif") and specifies exactly
what goes on screen; codegen turns that into a complete Manim CE file inside
one fenced python block. Artifacts land in ``runs/mythos/<timestamp>-<slug>/``.

The actual entry point is the ``math-to-manim`` command (``mythos.cli``).
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mythos.backends import (  # noqa: E402
    DEFAULT_COMMAND,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    BackendAuthError,
    is_claude_cli_command,
    model_fallbacks_from_env,
    run_model,
)
from mythos.charter import (  # noqa: E402
    CINEMATIC_CHARTER,
    JSON_CONTRACT,
    extract_json_object,
    extract_python_block,
    find_scene_class,
    load_env_file,
)

# Charter search order: user-local Claude Code dir first, then the tracked
# canonical copies that ship with the repo.
AGENT_DIRS = [
    REPO_ROOT / ".claude" / "agents",
    REPO_ROOT / "mythos" / "agents",
]

#: (slug, agent definition file, artifact name)
STAGES: list[tuple[str, str, str]] = [
    ("visual-brief", "mythos-visual-brief.md", "01_visual_brief.json"),
]

_LINT_RULES = [
    (r"self\.camera\.animate",
     "never animate self.camera in a ThreeDScene; use move_camera"),
]

#: Render wall-clock budget, separate from the per-model-call M2M_TIMEOUT.
DEFAULT_RENDER_TIMEOUT = float(os.getenv("M2M_RENDER_TIMEOUT", "1800"))


class StageValidationError(RuntimeError):
    """A reasoning stage returned a degenerate artifact twice in a row."""


def validate_stage_artifact(slug: str, artifact: dict) -> str | None:
    """Reject degenerate stage output before it poisons the chain.

    Returns a human-readable problem description, or None when healthy.
    (A real failure mode: the old math-director stage once returned
    ``{"formulas": [], "color_identity": {}, "numbers": []}`` — 48 bytes —
    and every downstream agent faithfully storyboarded an empty film.)
    """
    if not isinstance(artifact, dict) or not artifact:
        return "artifact is not a non-empty JSON object"
    size = len(json.dumps(artifact))
    if slug == "visual-brief":
        formulas = artifact.get("formulas")
        if isinstance(formulas, list) and not formulas:
            return "visual brief has an empty 'formulas' list"
        medium = artifact.get("medium")
        if medium == "gif":
            shots = artifact.get("shots")
            # Catches a genuinely near-empty artifact, not a legitimately
            # tight short gif: ~1 shot per 3-5s, so a 15s gif can be as few
            # as 3 shots.
            if isinstance(shots, list) and len(shots) < 3:
                return f"gif shot list has only {len(shots)} beats"
        elif medium == "image":
            if not artifact.get("composition"):
                return "image medium but 'composition' is missing/empty"
        else:
            return f"medium is {medium!r}, expected 'image' or 'gif'"
        if size < 300:
            return f"visual brief is only {size} bytes"
    elif size < 200:
        return f"artifact is only {size} bytes"
    return None


def _requires_light_art_direction(prompt: str) -> bool:
    text = prompt.lower()
    positive = ("off-white", "archival", "paper", "warm paper")
    negative = ("no black", "not black", "never black", "non-black")
    return any(term in text for term in positive) and any(
        term in text for term in negative)


def default_runs_dir() -> Path:
    load_env_file()
    return Path(
        os.getenv("M2M_RUNS_DIR")
        or os.getenv("M2M2_RUNS_DIR")
        or str(REPO_ROOT / "runs")
    ) / "mythos"


def resolve_manim() -> list[str]:
    """Find the manim entry point, preferring the active interpreter's env.

    Order: M2M_MANIM env override, `<current python> -m manim` when manim is
    importable, then whatever is on PATH.
    """
    override = os.getenv("M2M_MANIM")
    if override:
        return [override]
    try:
        import manim  # noqa: F401
        return [sys.executable, "-m", "manim"]
    except ImportError:
        pass
    return [shutil.which("manim") or "manim"]


class MythosHarness:
    """Runs the full chain: one reasoning stage, codegen, verify, render, repair."""

    def __init__(
        self,
        *,
        command: str = DEFAULT_COMMAND,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        render_timeout: float = DEFAULT_RENDER_TIMEOUT,
        offline: bool = False,
        runs_dir: Path | None = None,
        model_fallbacks: tuple[str, ...] | None = None,
    ):
        self.command = command
        self.model = model
        self.timeout = timeout
        self.render_timeout = render_timeout
        self.offline = offline
        self.runs_dir = runs_dir or default_runs_dir()
        self.model_fallbacks = (tuple(model_fallbacks)
                                if model_fallbacks is not None
                                else model_fallbacks_from_env())
        self.fallbacks_used: list[dict] = []

    # ------------------------------------------------------------------ #
    # Model plumbing                                                      #
    # ------------------------------------------------------------------ #

    def _model(self, prompt: str, *, system_extra: str | None = None) -> str:
        """One model call, with an Anthropic fallback ladder on the Claude CLI.

        Auth failures raise immediately (a broken login is broken for every
        model). Model-specific failures walk M2M_MODEL_FALLBACKS through the
        same CLI subscription login; the first model that answers becomes the
        baseline for the rest of the run and the switch is recorded in the
        manifest.
        """
        try:
            return run_model(
                prompt,
                system_extra=system_extra,
                command=self.command,
                model=self.model,
                timeout=self.timeout,
            )
        except BackendAuthError:
            raise
        except RuntimeError as primary_exc:
            if not is_claude_cli_command(self.command):
                raise
            candidates = [m for m in self.model_fallbacks if m != self.model]
            if not candidates:
                raise
            last_exc: RuntimeError = primary_exc
            failed = self.model
            for fallback in candidates:
                reason = str(last_exc).splitlines()[0][:200]
                print(f"  [mythos] model {failed!r} failed ({reason}); "
                      f"falling back to {fallback!r}")
                try:
                    output = run_model(
                        prompt,
                        system_extra=system_extra,
                        command=self.command,
                        model=fallback,
                        timeout=self.timeout,
                    )
                except BackendAuthError:
                    raise
                except RuntimeError as exc:
                    last_exc = exc
                    failed = fallback
                    continue
                self.fallbacks_used.append({
                    "from": self.model,
                    "to": fallback,
                    "reason": str(primary_exc).splitlines()[0][:300],
                })
                self.model = fallback  # sticky for the rest of the run
                return output
            tried = [self.model, *candidates]
            raise RuntimeError(
                "All Anthropic models failed via the Claude CLI "
                f"(tried, in order: {', '.join(tried)}). Last error:\n"
                f"{last_exc}"
            ) from last_exc

    # ------------------------------------------------------------------ #
    # Charters                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def load_charter(agent_file: str) -> str:
        for candidate in AGENT_DIRS:
            if (candidate / agent_file).exists():
                path = candidate / agent_file
                break
        else:
            raise FileNotFoundError(
                f"No charter {agent_file!r} in any of: "
                + ", ".join(str(d) for d in AGENT_DIRS)
            )
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4 :]
        return text.strip()

    # ------------------------------------------------------------------ #
    # Run                                                                 #
    # ------------------------------------------------------------------ #

    def run(
        self,
        prompt: str,
        *,
        render: bool = False,
        quality: str = "l",
        max_repairs: int = 3,
        run_dir_callback=None,
    ) -> dict:
        prompt = prompt.replace(chr(0xFEFF), "").strip()
        run_dir = self._create_run_dir(prompt)
        if run_dir_callback is not None:
            run_dir_callback(run_dir.name)
        manifest: dict = {
            "run_id": run_dir.name,
            "prompt": prompt,
            "model": self.model,
            "command": self.command,
            "offline": self.offline,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "stages": [],
        }

        artifact: dict = {"user_prompt": prompt}
        for slug, agent_file, artifact_name in STAGES:
            started = time.time()
            retried = False
            if self.offline:
                artifact = _offline_artifact(slug, prompt, artifact)
            else:
                artifact, retried = self._run_stage(
                    slug, agent_file, artifact, run_dir, artifact_name)
            (run_dir / artifact_name).write_text(
                json.dumps(artifact, indent=2), encoding="utf-8")
            stage_record = {"stage": slug, "artifact": artifact_name,
                            "seconds": round(time.time() - started, 2)}
            if retried:
                stage_record["retried"] = True
            manifest["stages"].append(stage_record)
            manifest["model"] = self.model  # may have walked the fallback ladder
            if self.fallbacks_used:
                manifest["model_fallbacks"] = list(self.fallbacks_used)
            self._write_manifest(run_dir, manifest)
            print(f"  [mythos] {slug:<16} -> {artifact_name}")

        code_path, scene_name = self._codegen(run_dir, prompt, artifact, manifest)
        ok, failure = self._verify(code_path, prompt=prompt)
        manifest["static_check"] = {
            "passed": ok, "detail": failure[:2000] if failure else None}

        if render:
            attempt = 0
            while True:
                if ok:
                    rc, out = self._render(code_path, scene_name, quality,
                                           medium=manifest.get("medium", "gif"))
                    manifest.setdefault("renders", []).append(
                        {"attempt": attempt, "exit_code": rc})
                    if rc == 0:
                        break
                    failure = out
                    if rc == 124:
                        # A timeout is a budget problem, not a code problem —
                        # don't burn model-repair attempts on it.
                        manifest["render_timed_out"] = True
                        break
                if attempt >= max_repairs or self.offline:
                    break
                attempt += 1
                print(f"  [mythos] repair attempt {attempt}")
                code_path, scene_name = self._repair(
                    run_dir, code_path, failure or "unknown failure",
                    attempt, manifest)
                ok, failure = self._verify(code_path, prompt=prompt)

        manifest["scene_file"] = str(code_path)
        manifest["scene_name"] = scene_name
        manifest["model"] = self.model  # codegen/repair may have fallen back too
        if self.fallbacks_used:
            manifest["model_fallbacks"] = list(self.fallbacks_used)
        manifest["completed_utc"] = datetime.now(timezone.utc).isoformat()
        self._write_manifest(run_dir, manifest)
        print(f"  [mythos] run complete -> {run_dir}")
        return manifest

    # ------------------------------------------------------------------ #
    # Stage execution with degenerate-output retry                        #
    # ------------------------------------------------------------------ #

    def _run_stage(self, slug: str, agent_file: str, prior: dict,
                   run_dir: Path, artifact_name: str) -> tuple[dict, bool]:
        """Run one reasoning stage; retry once if the artifact is degenerate."""
        charter = self.load_charter(agent_file)
        base_prompt = (
            f"{charter}\n\nINPUT ARTIFACT JSON:\n"
            f"{json.dumps(prior, indent=2)}{JSON_CONTRACT}"
        )
        raw = self._model(base_prompt, system_extra=CINEMATIC_CHARTER)
        (run_dir / f"{artifact_name.split('.')[0]}.raw.txt").write_text(
            raw, encoding="utf-8")
        artifact = extract_json_object(raw)
        problem = validate_stage_artifact(slug, artifact)
        if problem is None:
            return artifact, False

        print(f"  [mythos] {slug}: degenerate output ({problem}); retrying")
        retry_prompt = (
            f"{base_prompt}\n\nPREVIOUS ATTEMPT REJECTED: your last artifact "
            f"was degenerate ({problem}). The input artifact above contains "
            "real material — mine it. Return a complete, richly populated "
            "JSON object this time; empty lists and placeholder text are "
            "failures."
        )
        raw = self._model(retry_prompt, system_extra=CINEMATIC_CHARTER)
        (run_dir / f"{artifact_name.split('.')[0]}.retry.raw.txt").write_text(
            raw, encoding="utf-8")
        artifact = extract_json_object(raw)
        problem = validate_stage_artifact(slug, artifact)
        if problem is not None:
            raise StageValidationError(
                f"Stage {slug!r} returned a degenerate artifact twice "
                f"({problem}). Aborting instead of filming an empty story. "
                f"Inspect {run_dir / artifact_name} and the .raw.txt traces."
            )
        return artifact, True

    # ------------------------------------------------------------------ #
    # Codegen / verify / render / repair                                  #
    # ------------------------------------------------------------------ #

    def _codegen(self, run_dir: Path, prompt: str, scene_spec: dict,
                 manifest: dict) -> tuple[Path, str]:
        started = time.time()
        if self.offline:
            code = _OFFLINE_SCENE
        else:
            medium = scene_spec.get("medium", "image")
            if medium == "image":
                medium_instructions = (
                    "MEDIUM: static image. Build the single frame with\n"
                    "self.add(...) only — no self.play, no camera movement, no\n"
                    "waits beyond what's needed for LaTeX to typeset. This file\n"
                    "will be rendered with `manim -s` (save last frame only), so\n"
                    "everything must be on screen and in its final state by the\n"
                    "end of construct()."
                )
            else:
                medium_instructions = (
                    "MEDIUM: short gif. Implement the 'shots' list as a short\n"
                    "Scene using self.play/self.wait per shot, following the\n"
                    "Cinematic Charter's camera and caption grammar. Total\n"
                    "runtime must match the sum of the shots' 'seconds' fields,\n"
                    "+/- 15%."
                )
            codegen_prompt = (
                "You are the code-generation stage: turn one visual brief into\n"
                "a complete, runnable Manim Community Edition Python file.\n\n"
                + medium_instructions + "\n\n"
                "The file must be self-contained (inline any helpers), import\n"
                "`from manim import *`, and define exactly one Scene subclass\n"
                "(ThreeDScene only if 3D is genuinely needed, otherwise plain\n"
                "Scene). Respond with exactly one fenced python block and\n"
                "nothing else.\n\nVISUAL BRIEF JSON:\n"
                + json.dumps(scene_spec, indent=2)
            )
            raw = self._model(codegen_prompt, system_extra=CINEMATIC_CHARTER)
            (run_dir / "02_codegen.raw.txt").write_text(raw, encoding="utf-8")
            code = extract_python_block(raw)
        code_path = run_dir / "mythos_scene.py"
        code_path.write_text(code, encoding="utf-8")
        scene_name = find_scene_class(code)
        manifest["stages"].append(
            {"stage": "codegen", "artifact": "mythos_scene.py",
             "seconds": round(time.time() - started, 2)})
        manifest["medium"] = scene_spec.get("medium", "image")
        self._write_manifest(run_dir, manifest)
        print(f"  [mythos] codegen          -> mythos_scene.py ({scene_name})")
        return code_path, scene_name

    @staticmethod
    def _verify(code_path: Path, *, prompt: str | None = None) -> tuple[bool, str | None]:
        try:
            py_compile.compile(str(code_path), doraise=True)
        except py_compile.PyCompileError as exc:
            return False, str(exc)
        code = code_path.read_text(encoding="utf-8")
        for pattern, message in _LINT_RULES:
            if re.search(pattern, code):
                return False, f"charter lint: {message}"
        if prompt and _requires_light_art_direction(prompt):
            dark_background_patterns = [
                r"background_color\s*=\s*[\"']#0c0c0b[\"']",
                r"background_color\s*=\s*BLACK\b",
                r"\bBG\s*=\s*[\"']#0c0c0b[\"']",
            ]
            for pattern in dark_background_patterns:
                if re.search(pattern, code, flags=re.IGNORECASE):
                    return False, (
                        "prompt art-direction lint: this prompt explicitly "
                        "requires an archival/off-white, non-black background; "
                        "replace the default dark Mythos background with warm "
                        "paper and use dark ink text/geometry."
                    )
        return True, None

    def _render(self, code_path: Path, scene_name: str,
                quality: str, medium: str = "gif") -> tuple[int, str]:
        # -s (save_last_frame): no video encode, just the final frame as a
        # PNG — the cheap, fast path for the "image" medium.
        flags = ["-s"] if medium == "image" else []
        cmd = resolve_manim() + flags + [f"-q{quality}", str(code_path), scene_name]
        print(f"  [mythos] rendering: {' '.join(cmd)}")
        try:
            completed = subprocess.run(
                cmd, text=True, capture_output=True,
                cwd=str(REPO_ROOT), timeout=self.render_timeout)
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stderr or b"")
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            return 124, (
                f"render timed out after {self.render_timeout:.0f}s "
                "(raise M2M_RENDER_TIMEOUT or lower the quality)\n" + partial
            )
        return completed.returncode, (completed.stderr or "") + (completed.stdout or "")

    def _repair(self, run_dir: Path, code_path: Path, failure: str,
                attempt: int, manifest: dict) -> tuple[Path, str]:
        prompt = (
            "The Manim scene below failed. Repair it surgically: preserve the\n"
            "cinematic structure, class name, and Charter rules; fix only what is\n"
            "broken. Manim CE APIs only. Respond with exactly one fenced\n"
            "python block containing the COMPLETE corrected file.\n\n"
            f"CURRENT FILE:\n{code_path.read_text(encoding='utf-8')}\n\n"
            f"FAILURE OUTPUT (tail):\n{failure[-8000:]}"
        )
        raw = self._model(prompt, system_extra=CINEMATIC_CHARTER)
        (run_dir / f"repair_{attempt}.raw.txt").write_text(raw, encoding="utf-8")
        code = extract_python_block(raw)
        code_path.write_text(code, encoding="utf-8")
        manifest["stages"].append({"stage": f"repair_{attempt}",
                                   "artifact": code_path.name})
        self._write_manifest(run_dir, manifest)
        return code_path, find_scene_class(code)

    # ------------------------------------------------------------------ #
    # Bookkeeping                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _write_manifest(run_dir: Path, manifest: dict) -> None:
        # Atomic: readers polling the run dir must never see a half-written
        # manifest (mid-write JSON parse errors were a real observed failure).
        # On Windows, os.replace onto a file a poller currently holds open
        # raises PermissionError — retry briefly, then fall back to a direct
        # write rather than killing the run over a progress file.
        payload = json.dumps(manifest, indent=2)
        target = run_dir / "manifest.json"
        tmp = run_dir / ".manifest.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(8):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
        target.write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)

    def _create_run_dir(self, prompt: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:48] or "run"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = self.runs_dir / f"{stamp}-{slug}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir


# ---------------------------------------------------------------------- #
# Offline mode                                                            #
# ---------------------------------------------------------------------- #

def _offline_artifact(slug: str, prompt: str, prior: dict) -> dict:
    """Deterministic stand-ins so the chain runs without a CLI login."""
    base = {"stage": slug, "topic": prompt, "offline": True,
            "prior_keys": sorted(prior)}
    if slug == "visual-brief":
        base["medium"] = "gif"
        base["formulas"] = [
            {"id": "f1", "latex_parts": ["e^{i\\pi}", "+1=0"], "color": "coral"},
        ]
        base["shots"] = [
            {"beat": 1, "move": "HEADLINE", "text": "A deterministic rehearsal."},
            {"beat": 2, "move": "ZOOM_IN", "target": "formula"},
            {"beat": 3, "move": "PULL_BACK"},
        ]
    return base


_OFFLINE_SCENE = '''from manim import *


class MythosOfflineScene(ThreeDScene):
    """Offline rehearsal scene: proves the harness plumbing end to end."""

    def construct(self):
        self.camera.background_color = "#0c0c0b"
        self.set_camera_orientation(phi=0 * DEGREES, theta=-90 * DEGREES)
        title = Text("Mythos harness: offline rehearsal", font_size=40, color="#faf9f5")
        formula = MathTex(r"e^{i\\pi} + 1 = 0", font_size=64, color="#d97757")
        self.play(FadeIn(title))
        self.wait(0.6)
        self.play(title.animate.scale(0.5).to_edge(UP), FadeIn(formula))
        self.move_camera(frame_center=formula.get_center(), zoom=2.2, run_time=1.2)
        self.wait(0.6)
        self.move_camera(frame_center=ORIGIN, zoom=1.0, run_time=1.0)
        self.play(FadeOut(formula), FadeOut(title))
'''
