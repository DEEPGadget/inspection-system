"""
Report Worker 유닛 테스트.
DB·WeasyPrint·openpyxl은 mock — 렌더링 컨텍스트 구성, 상태 전이, 에러 경로 검증.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _render_xlsx — 실제 openpyxl 호출 (파일 I/O 없이 메모리에서 검증)
# ---------------------------------------------------------------------------


def _make_context(overall: str = "pass") -> dict:
    return {
        "job_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "target_host": "10.0.0.1",
        "target_user": "root",
        "product_profile": "gpu_server",
        "created_at": "2026-03-25 12:00:00 UTC",
        "generated_at": "2026-03-25 12:05:00 UTC",
        "overall": overall,
        "fail_reasons": ["GPU 온도 92°C > 87°C"] if overall == "fail" else [],
        "warn_reasons": [],
        "summary": "테스트 요약",
        "check_results": [
            {
                "check_name": "sw_gpu",
                "status": "pass",
                "detail": "8x A100 OK",
                "claude_verdict": "[PASS] 정상",
            },
            {
                "check_name": "sw_power_mgmt",
                "status": "fail" if overall == "fail" else "pass",
                "detail": "sleep.target not masked",
                "claude_verdict": "[FAIL] 미마스킹" if overall == "fail" else "[PASS] 정상",
            },
        ],
    }


def test_render_xlsx_sheets(tmp_path):
    from workers.report import _render_xlsx

    out = tmp_path / "report.xlsx"
    _render_xlsx(_make_context("pass"), out)

    import openpyxl

    wb = openpyxl.load_workbook(str(out))
    assert "요약" in wb.sheetnames
    assert "검수 상세" in wb.sheetnames


def test_render_xlsx_summary_verdict(tmp_path):
    from workers.report import _render_xlsx

    out = tmp_path / "report.xlsx"
    _render_xlsx(_make_context("fail"), out)

    import openpyxl

    wb = openpyxl.load_workbook(str(out))
    ws = wb["요약"]
    values = [ws.cell(row=r, column=2).value for r in range(1, 8)]
    assert "FAIL" in values


def test_render_xlsx_detail_rows(tmp_path):
    from workers.report import _render_xlsx

    out = tmp_path / "report.xlsx"
    ctx = _make_context("pass")
    _render_xlsx(ctx, out)

    import openpyxl

    wb = openpyxl.load_workbook(str(out))
    ws = wb["검수 상세"]
    # 헤더(1) + 검수 항목(2)
    assert ws.max_row == 3
    assert ws.cell(row=2, column=1).value == "sw_gpu"


def test_render_xlsx_fail_reasons(tmp_path):
    from workers.report import _render_xlsx

    out = tmp_path / "report.xlsx"
    _render_xlsx(_make_context("fail"), out)

    import openpyxl

    wb = openpyxl.load_workbook(str(out))
    ws = wb["요약"]
    all_values = [ws.cell(row=r, column=2).value for r in range(1, ws.max_row + 1)]
    assert "GPU 온도 92°C > 87°C" in all_values


# ---------------------------------------------------------------------------
# _render_pdf — xelatex 호출 검증 (실제 컴파일 없이 mock)
# ---------------------------------------------------------------------------


def test_render_pdf_calls_xelatex(tmp_path):
    from workers.report import _render_pdf

    out = tmp_path / "report.pdf"
    with (
        patch("workers.report.subprocess.run") as mock_run,
        patch("workers.report.shutil.copy"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        _render_pdf(_make_context("pass"), out)

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "xelatex"
    assert "-interaction=nonstopmode" in cmd


def test_render_pdf_xelatex_failure(tmp_path):
    """xelatex 실패 시 RuntimeError 발생."""
    from workers.report import _render_pdf

    out = tmp_path / "report.pdf"
    with patch("workers.report.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="! LaTeX Error: something")
        with pytest.raises(RuntimeError, match="xelatex failed"):
            _render_pdf(_make_context("pass"), out)


def test_render_pdf_template_renders_without_error(tmp_path):
    """pass/fail 컨텍스트가 LaTeX 템플릿에 정상 렌더링되는지 확인 (컴파일 없이)."""
    from workers.report import _latex_env

    for overall in ("pass", "fail", "error"):
        tex_str = _latex_env.get_template("report.tex.j2").render(**_make_context(overall))
        assert r"\documentclass" in tex_str
        assert overall.upper() in tex_str.upper() or "ERROR" in tex_str


# ---------------------------------------------------------------------------
# _async_generate_report — DB·NFS·렌더러 모두 mock하여 흐름 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_generate_report_pass_flow(tmp_path):
    """pass 흐름: verdict 로드 → 렌더링 → DB 저장 → status=pass."""
    job_id = str(uuid.uuid4())
    verdict = {
        "overall": "pass",
        "fail_reasons": [],
        "warn_reasons": [],
        "summary": "All good",
        "checks": [],
    }

    # NFS verdict 파일 생성
    verdict_dir = tmp_path / "results" / job_id
    verdict_dir.mkdir(parents=True)
    (verdict_dir / "claude_verdict.json").write_text(json.dumps(verdict))

    fake_job = MagicMock()
    fake_job.id = uuid.UUID(job_id)
    fake_job.target_host = "10.0.0.1"
    fake_job.target_user = "root"
    fake_job.product_profile = "gpu_server"
    fake_job.created_at = MagicMock()
    fake_job.created_at.strftime.return_value = "2026-03-25 12:00:00 UTC"

    with (
        patch("workers.report.settings") as mock_settings,
        patch("workers.report._load_job_and_results", new_callable=AsyncMock) as mock_load,
        patch("workers.report._save_report_record", new_callable=AsyncMock) as mock_save,
        patch("workers.report._update_job_status", new_callable=AsyncMock) as mock_update,
        patch("workers.report._render_pdf") as mock_pdf,
        patch("workers.report._render_xlsx") as mock_xlsx,
        patch("workers.report._SessionLocal"),
    ):
        mock_settings.nfs_base_path = str(tmp_path)
        mock_load.return_value = (fake_job, [])

        from workers.report import _async_generate_report

        await _async_generate_report(job_id)

    mock_pdf.assert_called_once()
    mock_xlsx.assert_called_once()
    mock_save.assert_called_once()

    # 마지막 update 호출이 status="pass"인지 확인
    last_call_args = mock_update.call_args_list[-1]
    assert last_call_args.args[2] == "pass"


@pytest.mark.asyncio
async def test_async_generate_report_missing_verdict(tmp_path):
    """claude_verdict.json 없으면 FileNotFoundError 발생."""
    job_id = str(uuid.uuid4())

    fake_job = MagicMock()
    fake_job.id = uuid.UUID(job_id)
    fake_job.target_host = "10.0.0.1"
    fake_job.target_user = "root"
    fake_job.product_profile = "gpu_server"
    fake_job.created_at = MagicMock()
    fake_job.created_at.strftime.return_value = "2026-03-25 12:00:00 UTC"

    with (
        patch("workers.report.settings") as mock_settings,
        patch("workers.report._load_job_and_results", new_callable=AsyncMock) as mock_load,
        patch("workers.report._SessionLocal"),
    ):
        mock_settings.nfs_base_path = str(tmp_path)
        mock_load.return_value = (fake_job, [])

        from workers.report import _async_generate_report

        with pytest.raises(FileNotFoundError):
            await _async_generate_report(job_id)
