"""D110 이후 시험 보조 — 비용 추정이 없으니 승인이 묶을 값은 지금 잰 분석 범위에서 읽는다."""

from __future__ import annotations

import argparse
from pathlib import Path

from vlogsrt.config import load_config
from vlogsrt.project import commands as project_commands
from vlogsrt.project.final_caption import build_caption_estimate, caption_settings_sha256
from vlogsrt.project.workflow_protocol import (
    AnalysisAuthorization,
    AnalysisEstimate,
    CaptionEstimate,
    WorkflowJournal,
)


def scope_of(
    workspace: Path,
    *,
    project_config: Path | None = None,
    media_map: Path | None = None,
) -> AnalysisEstimate:
    """작업공간의 저널 리비전으로 `analyze`가 다시 잴 분석 범위를 지금 잰다(파일은 쓰지 않는다)."""
    journal = WorkflowJournal.open(workspace / project_commands._JOURNAL)
    args = argparse.Namespace(project_config=project_config, media_map=media_map)
    return project_commands._current_analysis_estimate(args, workspace, journal.revision)


def authorization_for(
    workspace: Path,
    *,
    project_config: Path | None = None,
    media_map: Path | None = None,
    authorized_at: str = "2026-09-27T04:00:00Z",
) -> AnalysisAuthorization:
    scope = scope_of(workspace, project_config=project_config, media_map=media_map)
    return AnalysisAuthorization(
        input_revision=scope.input_revision,
        settings_sha256=scope.settings_sha256,
        media_grant_revision=scope.media_grant_revision,
        authorized_at=authorized_at,
    )


def caption_scope_of(job_dir: Path) -> CaptionEstimate:
    """`caption-run`이 다시 잴 자막 범위를 지금 잰다(파일은 쓰지 않는다)."""
    request, validated = project_commands._caption_job_facts(job_dir)
    return build_caption_estimate(
        request,
        audio_seconds=validated.audio_seconds,
        settings_sha256=caption_settings_sha256(load_config()),
    )
