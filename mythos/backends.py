"""Model backend for the Mythos chain: the Claude CLI.

    claude -p --model claude-fable-5

Secrets are read from environment variables only and are never stamped into
artifacts or error messages.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from mythos.charter import load_env_file, resolve_command

# ----------------------------------------------------------------------- #
# Configuration: environment first, sane defaults second.                  #
#                                                                          #
#   M2M_MODEL           baseline model id      (default: claude-fable-5)   #
#   M2M_COMMAND         backend                (default: claude)           #
#   M2M_TIMEOUT         per-model-call seconds (default: 900)              #
#                                                                          #
# Values may live in the environment or in a local (gitignored) .env.      #
# ----------------------------------------------------------------------- #
load_env_file()

DEFAULT_MODEL = os.getenv("M2M_MODEL") or "claude-fable-5"
DEFAULT_COMMAND = os.getenv("M2M_COMMAND", "claude")
DEFAULT_TIMEOUT = float(os.getenv("M2M_TIMEOUT", "900"))

#: Anthropic models tried, in order, when the primary fails for a
#: model-specific reason (overload, not-found, 5xx). All of them run through
#: the same Claude CLI subscription login — no API key involved. Haiku is
#: deliberately absent: it is below the quality bar for reasoning stages.
_FALLBACKS_DEFAULT = "claude-opus-4-8,claude-sonnet-5"


def model_fallbacks_from_env() -> tuple[str, ...]:
    """Fallback model ids from M2M_MODEL_FALLBACKS (comma-separated).

    Set it to an empty string to disable fallback entirely.
    """
    load_env_file()
    raw = os.getenv("M2M_MODEL_FALLBACKS")
    if raw is None:
        raw = _FALLBACKS_DEFAULT
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def is_claude_cli_command(command: str) -> bool:
    """True — the only backend this fork supports is the Claude CLI."""
    return True


class BackendAuthError(RuntimeError):
    """The model backend is reachable but refuses our credentials."""


#: Substrings that mark an authentication failure in CLI output.
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "failed to authenticate",
    "invalid authentication credentials",
    "authentication_error",
    "api error: 401",
    "http 401",
    "401 unauthorized",
)

#: Windows CreateProcess command lines top out near 32k chars; leave headroom.
_ARGV_PROMPT_LIMIT = 28000

#: Access-violation exit codes from a flaky Windows CLI, worth retrying.
_CRASH_EXIT_CODES = {3221225477, -1073741819}
_CLI_CRASH_RETRIES = 3


def run_model(
    prompt: str,
    *,
    system_extra: str | None = None,
    command: str = DEFAULT_COMMAND,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Send one prompt to the Claude CLI and return raw text output."""
    resolved = resolve_command(command)
    cmd = [resolved, "-p", "--output-format", "text", "--model", model]
    if system_extra:
        cmd += ["--append-system-prompt", system_extra]
    stdin_prompt: str | None = prompt
    if os.name == "nt" and len(prompt) < _ARGV_PROMPT_LIMIT:
        # The Windows Claude CLI can crash reading piped stdin; pass the
        # prompt as an argument when it fits in the command line.
        cmd.insert(2, prompt)
        stdin_prompt = None
    # This repo runs the chain on the Claude CLI's subscription login. A
    # stray ANTHROPIC_API_KEY in the environment would silently override
    # that login (and 401 if stale), so scrub it.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    # The Windows Claude CLI intermittently dies with an access violation
    # (0xC0000005); retry those crashes a few times.
    for attempt in range(_CLI_CRASH_RETRIES + 1):
        completed = subprocess.run(
            cmd, input=stdin_prompt, text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=env,
            timeout=timeout, check=False,
        )
        if completed.returncode not in _CRASH_EXIT_CODES:
            break
        time.sleep(1.5 * (attempt + 1))
    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        if any(marker in combined for marker in _AUTH_MARKERS):
            raise BackendAuthError(
                f"The {Path(cmd[0]).stem!r} backend rejected our credentials.\n"
                "Run `claude /login` (the chain uses the CLI's subscription "
                "login; a stale ANTHROPIC_API_KEY is scrubbed on purpose).\n"
                f"backend output (tail):\n{combined[-1200:]}"
            )
        raise RuntimeError(
            f"Mythos model command failed (exit {completed.returncode})\n"
            f"command: {cmd[0]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    stripped = (completed.stdout or "").strip().lower()
    if stripped.startswith("not logged in"):
        raise BackendAuthError(
            f"The {Path(cmd[0]).stem!r} backend is not logged in. "
            "Run `claude /login`."
        )
    return completed.stdout
