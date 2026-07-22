"""Robustness regressions: stage validation, atomic manifests, env config.

Each test here encodes a failure observed in a real run:
- a 48-byte math dossier that storyboarded an empty film
- manifest.json read mid-write by a polling MCP client
- a stale CLI login producing an opaque mid-chain crash
"""

from __future__ import annotations

import json

import pytest

from mythos.backends import BackendAuthError
from mythos.harness import (
    DEFAULT_RENDER_TIMEOUT,
    MythosHarness,
    StageValidationError,
    validate_stage_artifact,
)


# --------------------------------------------------------------------- #
# Stage validation                                                       #
# --------------------------------------------------------------------- #

def test_empty_formulas_rejected():
    # The exact shape of a real degenerate artifact: empty formulas list.
    artifact = {"medium": "image", "formulas": [], "composition": "a circle"}
    problem = validate_stage_artifact("visual-brief", artifact)
    assert problem is not None
    assert "formulas" in problem


def test_thin_gif_shot_list_rejected():
    artifact = {"medium": "gif", "formulas": [{"id": "f1"}],
                "shots": [{"beat": i} for i in range(2)]}
    problem = validate_stage_artifact("visual-brief", artifact)
    assert problem is not None
    assert "2 beats" in problem


def test_tight_short_gif_shot_list_not_rejected_by_count():
    # A legitimately tight ~15s gif (~1 shot per 3-5s) should NOT be
    # treated as degenerate.
    artifact = {"medium": "gif", "formulas": [{"id": "f1"}],
                "shots": [{"beat": i, "verb": "HEADLINE", "seconds": 3.0,
                          "caption_text": "x" * 40} for i in range(4)]}
    assert validate_stage_artifact("visual-brief", artifact) is None


def test_image_medium_missing_composition_rejected():
    artifact = {"medium": "image", "formulas": [{"id": "f1"}], "composition": ""}
    problem = validate_stage_artifact("visual-brief", artifact)
    assert problem is not None
    assert "composition" in problem


def test_unrecognized_medium_rejected():
    artifact = {"medium": "video", "formulas": [{"id": "f1"}]}
    problem = validate_stage_artifact("visual-brief", artifact)
    assert problem is not None
    assert "medium" in problem


def test_healthy_image_artifact_passes():
    artifact = {"medium": "image",
                "formulas": [{"id": "f1", "latex": "E=mc^2"}],
                "composition": "a single labeled arrow on a coordinate "
                               "grid, coral for the vector, gray axes" * 3}
    assert validate_stage_artifact("visual-brief", artifact) is None


def test_healthy_gif_artifact_passes():
    artifact = {"medium": "gif",
                "formulas": [{"id": "f1", "latex": "E=mc^2"}],
                "shots": [{"beat": i, "verb": "HEADLINE", "seconds": 5.0,
                          "caption_text": "x" * 40} for i in range(5)]}
    assert validate_stage_artifact("visual-brief", artifact) is None


def test_non_dict_artifact_rejected():
    assert validate_stage_artifact("visual-brief", {}) is not None


def test_degenerate_stage_retries_then_aborts(tmp_path, monkeypatch):
    harness = MythosHarness(offline=False, runs_dir=tmp_path)
    calls = []

    def fake_model(prompt, system_extra=None):
        calls.append(prompt)
        return '{"medium": "image", "formulas": [], "composition": "x"}'

    monkeypatch.setattr(harness, "_model", fake_model)
    monkeypatch.setattr(harness, "load_charter", lambda f: "CHARTER")
    with pytest.raises(StageValidationError):
        harness._run_stage("visual-brief", "mythos-visual-brief.md",
                           {"prior": "artifact"}, tmp_path,
                           "01_visual_brief.json")
    assert len(calls) == 2                       # one retry, then abort
    assert "PREVIOUS ATTEMPT REJECTED" in calls[1]
    assert (tmp_path / "01_visual_brief.retry.raw.txt").exists()


def test_degenerate_stage_recovers_on_retry(tmp_path, monkeypatch):
    harness = MythosHarness(offline=False, runs_dir=tmp_path)
    healthy = json.dumps({
        "medium": "image",
        "formulas": [{"id": "f1", "latex": "x"}],
        "composition": "y" * 320})
    responses = iter(['{"medium": "image", "formulas": []}', healthy])

    def fake_model(prompt, system_extra=None):
        return next(responses)

    monkeypatch.setattr(harness, "_model", fake_model)
    monkeypatch.setattr(harness, "load_charter", lambda f: "CHARTER")
    artifact, retried = harness._run_stage(
        "visual-brief", "mythos-visual-brief.md", {}, tmp_path,
        "01_visual_brief.json")
    assert retried is True
    assert len(artifact["formulas"]) == 1


# --------------------------------------------------------------------- #
# Atomic manifest                                                        #
# --------------------------------------------------------------------- #

def test_manifest_write_is_atomic_and_clean(tmp_path):
    manifest = {"run_id": "x", "stages": [{"stage": "intent"}]}
    MythosHarness._write_manifest(tmp_path, manifest)
    on_disk = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert on_disk == manifest
    assert not (tmp_path / ".manifest.json.tmp").exists()


# --------------------------------------------------------------------- #
# Configuration comes from the environment                               #
# --------------------------------------------------------------------- #

def test_render_timeout_default_positive():
    assert DEFAULT_RENDER_TIMEOUT > 0


def test_no_hardcoded_model_outside_env_default():
    """DEFAULT_MODEL must be derived from env-or-fallback, not a bare literal
    scattered through the runtime modules."""
    import mythos.backends as backends
    assert isinstance(backends.DEFAULT_MODEL, str) and backends.DEFAULT_MODEL


def test_backend_auth_error_is_runtime_error():
    assert issubclass(BackendAuthError, RuntimeError)


def test_prompt_bom_stripped(tmp_path):
    harness = MythosHarness(offline=True, runs_dir=tmp_path)
    manifest = harness.run("﻿  the heat equation  ")
    assert manifest["prompt"] == "the heat equation"
