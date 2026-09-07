"""
Job lifecycle: pending -> claimed -> running -> awaiting_cloud_steps ->
succeeded/failed.

The admin-team client (authenticated with a "client" key) creates a job -
either from an already-parsed dict, or by uploading the joiner Excel file,
which this module parses using Config.excel_field_map. The connector
(authenticated with a "connector" key) polls for pending work, claims it,
and does the on-prem AD steps. Once the on-prem account exists, it hands
back samaccountname/UPN and a background task takes over for the
Graph-only steps (group membership, licensing), since those are plain
REST calls that don't need an on-prem hop and can take several minutes
waiting for Entra sync.

Single-tenant: there's exactly one Config row and one queue of jobs - no
tenant_id anywhere, since this whole deployment serves one customer.
"""
from datetime import datetime, timezone
from typing import Optional

import openpyxl
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from sqlmodel import Session, select

from ..auth import require_client, require_connector
from ..db import get_session
from ..models import Config, Job, JobStatus, SINGLETON_ID
from ..graph_actions import run_cloud_steps

router = APIRouter(tags=["jobs"])

# A real joiner form is tens of KB. 10MB is generous headroom while still
# capping how much a single upload can force into memory - `await
# file.read()` loads the whole thing at once, so an unbounded upload is a
# cheap way to pressure the process's memory.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _read_upload_with_limit(file: UploadFile) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_config_or_400(session: Session) -> Config:
    config = session.get(Config, SINGLETON_ID)
    if config is None:
        raise HTTPException(status_code=400, detail="Not configured yet - PUT /config first")
    return config


@router.get("/connector/config", response_model=Config)
def get_connector_config(_actor: str = Depends(require_connector), session: Session = Depends(get_session)):
    """The connector fetches config on every poll cycle (or on claiming a
    job) rather than caching it locally, so an admin editing site/group/OU
    values takes effect on the very next joiner without touching anything
    installed on-prem.
    """
    return _get_config_or_400(session)


@router.post("/jobs", response_model=Job)
def create_job(joiner_fields: dict, _actor: str = Depends(require_client), session: Session = Depends(get_session)):
    job = Job(joiner_fields=joiner_fields, status=JobStatus.pending)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _parse_excel_fields(file_bytes: bytes, excel_field_map: dict) -> dict:
    """Reads the cells this deployment's config says matter (e.g.
    {"full_name": "E6"}) out of an uploaded joiner form. A differently
    laid-out template just needs a different excel_field_map - this
    parsing code doesn't change.
    """
    import io

    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    sheet = workbook.active

    fields = {}
    for field_name, cell_ref in excel_field_map.items():
        value = sheet[cell_ref].value
        fields[field_name] = str(value).strip() if value is not None else ""
    return fields


@router.post("/jobs/from-excel", response_model=Job)
async def create_job_from_excel(
    file: UploadFile,
    _actor: str = Depends(require_client),
    session: Session = Depends(get_session),
):
    config = _get_config_or_400(session)
    if not config.excel_field_map:
        raise HTTPException(status_code=400, detail="No excel_field_map configured yet")

    file_bytes = await _read_upload_with_limit(file)
    try:
        joiner_fields = _parse_excel_fields(file_bytes, config.excel_field_map)
    except Exception as exc:  # noqa: BLE001 - surfacing the real cause to the caller is the point
        raise HTTPException(status_code=400, detail=f"Could not read joiner form: {exc}")

    job = Job(joiner_fields=joiner_fields, status=JobStatus.pending)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.post("/preview-excel")
async def preview_excel(
    file: UploadFile,
    _actor: str = Depends(require_client),
    session: Session = Depends(get_session),
):
    """Parses a joiner form WITHOUT creating a job - lets a client GUI show
    "detected joiner: X" and auto-suggest a logon name for a human to
    verify/edit before anything is actually submitted. Mirrors the
    original desktop tool's behaviour, where the logon name always came
    from a human checking against the HR system of record, never straight
    from the Excel file.
    """
    config = _get_config_or_400(session)
    if not config.excel_field_map:
        raise HTTPException(status_code=400, detail="No excel_field_map configured yet")

    file_bytes = await _read_upload_with_limit(file)
    try:
        return _parse_excel_fields(file_bytes, config.excel_field_map)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not read joiner form: {exc}")


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: int, _actor: str = Depends(require_client), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/connector/jobs/claim", response_model=Optional[Job])
def claim_next_job(_actor: str = Depends(require_connector), session: Session = Depends(get_session)):
    """Called by the connector's poll loop. Returns the oldest pending job,
    atomically marked as claimed so two connector instances (or a retry)
    can't double-process the same joiner.
    """
    job = session.exec(select(Job).where(Job.status == JobStatus.pending).order_by(Job.created_at)).first()
    if job is None:
        return None

    job.status = JobStatus.claimed
    job.claimed_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _get_job_or_404(job_id: int, session: Session) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/connector/jobs/{job_id}/log")
def append_log(
    job_id: int,
    line: str,
    _actor: str = Depends(require_connector),
    session: Session = Depends(get_session),
):
    job = _get_job_or_404(job_id, session)
    job.status = JobStatus.running
    job.log_lines = [*job.log_lines, line]
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()
    return {"ok": True}


@router.post("/connector/jobs/{job_id}/onprem-complete")
def onprem_complete(
    job_id: int,
    ad_samaccountname: str,
    ad_upn: str,
    background_tasks: BackgroundTasks,
    _actor: str = Depends(require_connector),
    session: Session = Depends(get_session),
):
    """Connector calls this once New-ADUser succeeded. Cloud steps (group
    membership, licensing) run as a background task rather than blocking
    this response - Entra sync can take several minutes, and there's no
    reason to hold the connector's HTTP connection open for that.
    """
    job = _get_job_or_404(job_id, session)
    job.ad_samaccountname = ad_samaccountname
    job.ad_upn = ad_upn
    job.status = JobStatus.awaiting_cloud_steps
    session.add(job)
    session.commit()

    background_tasks.add_task(run_cloud_steps, job.id)
    return {"ok": True}


@router.post("/connector/jobs/{job_id}/fail")
def fail_job(
    job_id: int,
    error_message: str,
    _actor: str = Depends(require_connector),
    session: Session = Depends(get_session),
):
    job = _get_job_or_404(job_id, session)
    job.status = JobStatus.failed
    job.error_message = error_message
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()
    return {"ok": True}
