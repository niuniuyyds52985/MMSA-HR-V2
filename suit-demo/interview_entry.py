"""Local-only structured interview API, independent from legacy emotion inference."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
import uuid

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from interview_core import BASE, DomainError, InterviewCore
from interview_runtime import build_identity, maintenance_active
from interview_workbench import operator_token_valid

MAX_UPLOAD = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".webm", ".wav", ".m4a", ".mp3", ".ogg"}
core = InterviewCore(config_path=Path(__file__).with_name('config') / 'interview.v4.json')
# Capture once: a running process must never claim it loaded later disk edits.
BUILD_ID = build_identity()
app = FastAPI(title="真才实鉴 · 答案证据复核", version="1.0.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])


class MaintenanceState:
    active_requests = 0
    active_write_requests = 0


maintenance_state = MaintenanceState()


class MaintenanceGate:
    """Drain complete ASGI requests before a controlled local restart.

    Some GET endpoints cache reports or visual artifacts, so all /v1/ calls
    participate. Health and static pages remain available during maintenance.
    Production runs one ASGI process; no await separates gate check and count.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        write = scope.get('method', 'GET') not in ('GET', 'HEAD', 'OPTIONS')
        protected = scope['type'] == 'http' and (write or scope.get('path', '').startswith('/v1/'))
        if not protected:
            await self.app(scope, receive, send)
            return
        if maintenance_active(core.data_dir):
            response = JSONResponse(status_code=503, headers={'Retry-After': '3'}, content={
                'detail': {'code': 'maintenance', 'message': '本机服务正在应用更新，请稍后重试。'}})
            await response(scope, receive, send)
            return
        maintenance_state.active_requests += 1
        maintenance_state.active_write_requests += int(write)
        try:
            await self.app(scope, receive, send)
        finally:
            maintenance_state.active_requests -= 1
            maintenance_state.active_write_requests -= int(write)


app.add_middleware(MaintenanceGate)


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        expected = urlsplit(str(request.base_url))
        if parsed.scheme not in ("http", "https") or parsed.hostname != expected.hostname or parsed.port != expected.port:
            return JSONResponse(status_code=403, content={"detail": {"code": "origin_forbidden", "message": "仅允许同源本地访问"}})
    length = request.headers.get("content-length", "0")
    try:
        if int(length) > MAX_UPLOAD + 1024 * 1024:
            return JSONResponse(status_code=413, content={"detail": {"code": "upload_too_large", "message": "上传不得超过100MB"}})
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": {"code": "invalid_content_length", "message": "无效请求长度"}})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.exception_handler(DomainError)
async def domain_exception(request: Request, error: DomainError):
    return JSONResponse(status_code=error.status, content={"detail": {"code": error.code, "message": error.message}})


def authorize(session_id: str, authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise DomainError("unauthorized", "需要此会话的访问凭证", 401)
    return core.authenticate(session_id, authorization[7:])


def hr_role(role=Depends(authorize)):
    if role != "hr":
        raise DomainError("forbidden", "需要HR复核凭证", 403)
    return role


def candidate_role(role=Depends(authorize)):
    if role != "candidate":
        raise DomainError("forbidden", "需要候选人作答凭证", 403)
    return role


def operator_role(authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.startswith('Bearer '):
        raise DomainError('unauthorized', '需要本机工作台管理凭证', 401)
    if not operator_token_valid(authorization[7:]):
        raise DomainError('forbidden', '该凭证无权管理工作台', 403)
    return 'operator'


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(StrictBody):
    form_mode: Literal["quick", "full"] | None = None
    candidate_ref: str = Field(default="", max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)


class NextBody(StrictBody):
    round_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


class RevisionBody(StrictBody):
    version: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=30000)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class EvidenceBody(StrictBody):
    answer_id: str
    transcript_revision: StrictInt = Field(ge=1)
    quote: str = Field(min_length=1, max_length=30000)
    segment_id: str | None = None
    char_start: StrictInt | None = Field(default=None, ge=0)
    char_end: StrictInt | None = Field(default=None, ge=1)


class RatingBody(StrictBody):
    question_id: str
    score: StrictInt | None = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[EvidenceBody] = Field(default_factory=list, max_length=20)


class ReviewBody(StrictBody):
    version: StrictInt = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    ratings: list[RatingBody] = Field(min_length=1, max_length=8)
    reviewer: str = Field(default="local_hr", max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


class AssessmentBody(StrictBody):
    version: StrictInt = Field(ge=1)
    status: str
    branch_id: str | None = None
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[EvidenceBody] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=200)


class AnalyzeBody(StrictBody):
    idempotency_key: str = Field(min_length=8, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class UploadInitBody(StrictBody):
    round_id: str = Field(min_length=1, max_length=100)
    attempt_id: str = Field(min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=255)
    total_size: StrictInt = Field(ge=1, le=MAX_UPLOAD)
    chunk_size: StrictInt = Field(default=1048576, ge=65536, le=4194304)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=8, max_length=200)


class UploadCompleteBody(StrictBody):
    transcript: str = Field(default='', max_length=30000)
    language: str = Field(default='zh', max_length=20)
    replace_answer_id: str | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


class StandardRetryBody(StrictBody):
    run_id: str = Field(min_length=1, max_length=100)
    version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ContentRetryBody(StrictBody):
    question_id: str = Field(min_length=1, max_length=100)
    version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class StandardResumeBody(StrictBody):
    version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ResearchSelection(StrictBody):
    answer_id: str = Field(min_length=1, max_length=100)
    transcript_revision: StrictInt = Field(ge=1)


class ResearchCreateBody(StrictBody):
    version: StrictInt = Field(ge=1)
    selections: list[ResearchSelection] = Field(min_length=1, max_length=4)
    modules: list[str] = Field(min_length=1, max_length=6)
    repeats: StrictInt = Field(default=1, ge=1, le=3)
    purpose: str = Field(min_length=1, max_length=2000)
    condition: str = Field(min_length=1, max_length=1000)
    participant_group: str = Field(min_length=1, max_length=100)
    cloud_consent: StrictBool = False
    experiment_id: str | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


class AnnotationBody(StrictBody):
    experiment_id: str = Field(min_length=1, max_length=100)
    rater: str = Field(min_length=1, max_length=100)
    exposure: str
    ratings: list[RatingBody] = Field(min_length=1, max_length=3)
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ReferenceBody(StrictBody):
    version: StrictInt = Field(ge=1)
    targets: dict[str, StrictInt]
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=2000)
    status: str
    idempotency_key: str = Field(min_length=8, max_length=200)


class CompareBody(StrictBody):
    run_ids: list[str] = Field(min_length=1, max_length=6)


class PublishBody(CompareBody):
    version: StrictInt = Field(ge=1)
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class AlignmentSegment(StrictBody):
    segment_id: str = Field(min_length=1, max_length=100)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(ge=1)


class AlignmentBody(StrictBody):
    revision: StrictInt = Field(ge=1)
    segments: list[AlignmentSegment] = Field(min_length=1, max_length=1000)
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=200)


def research_store():
    from interview_research import ResearchStore
    if not hasattr(core, '_research_store'):
        core._research_store = ResearchStore(core)
    return core._research_store


def upload_store():
    from interview_uploads import UploadStore
    if not hasattr(core, '_upload_store'):
        core._upload_store = UploadStore(core)
    return core._upload_store


@app.post('/v1/interviews/{session_id}/uploads')
def upload_init(session_id: str, body: UploadInitBody, role=Depends(candidate_role)):
    return upload_store().create(session_id, **body.model_dump())


@app.get('/v1/interviews/{session_id}/uploads/{upload_id}')
def upload_status(session_id: str, upload_id: str, role=Depends(candidate_role)):
    return upload_store().status(session_id, upload_id)


@app.put('/v1/interviews/{session_id}/uploads/{upload_id}/chunks/{index}')
async def upload_chunk(session_id: str, upload_id: str, index: int, request: Request,
                       x_chunk_sha256: Annotated[str, Header()], role=Depends(candidate_role)):
    from interview_uploads import MAX_CHUNK
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_CHUNK:
            raise DomainError('chunk_too_large', '单片不得超过4MB', 413)
        data.extend(chunk)
    return upload_store().put_chunk(session_id, upload_id, index, bytes(data), x_chunk_sha256)


@app.post('/v1/interviews/{session_id}/uploads/{upload_id}/complete')
def upload_complete(session_id: str, upload_id: str, body: UploadCompleteBody, role=Depends(candidate_role)):
    return upload_store().complete(session_id, upload_id, **body.model_dump())


@app.get('/v1/interviews/{session_id}/analysis-status')
def analysis_status(session_id: str, include_results: bool = False, role=Depends(hr_role)):
    return research_store().analysis_status(session_id, include_results=include_results)


@app.post('/v1/interviews/{session_id}/standard-analysis/retry')
def standard_retry(session_id: str, body: StandardRetryBody, role=Depends(hr_role)):
    return research_store().retry_standard(session_id, **body.model_dump())


@app.post('/v1/interviews/{session_id}/content-analysis/retry')
def content_retry(session_id: str, body: ContentRetryBody, role=Depends(hr_role)):
    return core.retry_content_analysis(session_id, **body.model_dump())


@app.post('/v1/interviews/{session_id}/standard-analysis/resume')
def standard_resume(session_id: str, body: StandardResumeBody, role=Depends(hr_role)):
    return research_store().resume_standard(session_id, **body.model_dump())


@app.get('/v1/interviews/{session_id}/factor-profile')
def potential_profile(session_id: str, report_version: int | None = Query(default=None, ge=1), role=Depends(hr_role)):
    from interview_profile import factor_profile
    report = core.get_report(session_id, report_version)
    return report.get('potential_profile') or factor_profile(report)


@app.get('/v1/interviews/{session_id}/research/runs')
def research_runs(session_id: str, role=Depends(hr_role)):
    return research_store().list_runs(session_id)


@app.post('/v1/interviews/{session_id}/research/runs')
def research_create(session_id: str, body: ResearchCreateBody, role=Depends(hr_role)):
    return research_store().create_runs(session_id, **body.model_dump())


@app.get('/v1/interviews/{session_id}/research/runs/{run_id}')
def research_run(session_id: str, run_id: str, role=Depends(hr_role)):
    return research_store().get_run(session_id, run_id)


@app.get('/v1/interviews/{session_id}/research/annotations')
def research_annotations(session_id: str, role=Depends(hr_role)):
    return research_store().annotations(session_id)


@app.post('/v1/interviews/{session_id}/research/annotations')
def research_annotate(session_id: str, body: AnnotationBody, role=Depends(hr_role)):
    return research_store().annotate(session_id, **body.model_dump())


@app.post('/v1/interviews/{session_id}/reference-profiles')
def reference_profile(session_id: str, body: ReferenceBody, role=Depends(hr_role)):
    return research_store().reference(session_id, **body.model_dump())


@app.post('/v1/interviews/{session_id}/research/compare')
def research_compare(session_id: str, body: CompareBody, role=Depends(hr_role)):
    return research_store().compare(session_id, body.run_ids)


@app.post('/v1/interviews/{session_id}/research/publish')
def research_publish(session_id: str, body: PublishBody, role=Depends(hr_role)):
    return research_store().publish(session_id, **body.model_dump())


@app.get('/v1/interviews/{session_id}/research/export')
def research_export(session_id: str, role=Depends(hr_role)):
    return research_store().export(session_id)


@app.post('/v1/interviews/{session_id}/answers/{answer_id}/alignment-reviews')
def alignment_review(session_id: str, answer_id: str, body: AlignmentBody, role=Depends(hr_role)):
    return research_store().alignment(session_id, answer_id=answer_id, **body.model_dump())


@app.get("/health")
def health():
    return {"service": "zhenjian-interview", "status": "ok", "config_version": core.config["version"],
            **BUILD_ID, 'maintenance_supported': True, 'maintenance_active': maintenance_active(core.data_dir),
            'active_requests': maintenance_state.active_requests,
            'active_write_requests': maintenance_state.active_write_requests}


@app.get("/")
@app.get("/interview")
def interview_page():
    return FileResponse(BASE / "site" / "interview.html")


@app.get("/hr-review")
def hr_page():
    return FileResponse(BASE / "site" / "hr-review.html")


@app.get('/workbench')
def workbench_page():
    return FileResponse(BASE / 'site' / 'workbench.html')


@app.get('/interview-report')
def report_page():
    return FileResponse(BASE / 'site' / 'interview-report.html')


@app.get('/research')
def research_page():
    return FileResponse(BASE / 'site' / 'research.html')


@app.get("/static/{subpath:path}")
def static_asset(subpath: str):
    if subpath not in {"css/interview.css", "js/interview.js", "js/hr-review.js",
                       "css/workbench.css", "js/workbench.js", "css/interview-report.css", "js/interview-report.js",
                       "css/visual-evidence.css", "js/visual-evidence.js",
                       "css/evidence-timeline.css", "js/evidence-timeline.js",
                       "css/research.css", "js/research.js", "css/potential-profile.css", "js/potential-profile.js", "js/research-summary.js", "js/alignment-editor.js",
                       "js/observation-context.js", "js/resumable-upload.js", "css/report-reader.css", "js/report-reader.js", "js/text-display.js", "css/candidate-flow.css", "css/review-workspace.css", "css/multimodal-view.css", "js/multimodal-view.js"}:
        raise DomainError("not_found", "文件不存在", 404)
    return FileResponse(BASE / "site" / subpath)


@app.get('/v1/workbench/sessions')
def workbench_sessions(role=Depends(operator_role)):
    return core.list_workbench_sessions()


@app.post('/v1/workbench/sessions/{session_id}/open')
def workbench_open(session_id: str, role=Depends(operator_role)):
    return core.workbench_session_links(session_id)


@app.post('/v1/workbench/sessions/{session_id}/analyze')
def workbench_analyze(session_id: str, body: AnalyzeBody, role=Depends(operator_role)):
    return core.authorize_cloud_analysis(session_id, **body.model_dump())


@app.post("/v1/interviews")
def create(body: CreateBody):
    return core.create_interview(**body.model_dump())


@app.get("/v1/interviews/{session_id}")
def state(session_id: str, role=Depends(authorize)):
    return core.get_state(session_id, hr=role == "hr")


@app.post("/v1/interviews/{session_id}/next")
def next_round(session_id: str, body: NextBody, role=Depends(candidate_role)):
    return core.next_round(session_id, **body.model_dump())


@app.post("/v1/interviews/{session_id}/answers")
def answer(session_id: str, round_id: Annotated[str, Form()], attempt_id: Annotated[str, Form()], idempotency_key: Annotated[str, Form()],
           transcript: Annotated[str, Form()] = "", language: Annotated[str, Form()] = "zh", replace_answer_id: Annotated[str | None, Form()] = None,
           file: Annotated[UploadFile | None, File()] = None, role=Depends(candidate_role)):
    path, meta, media_hash = None, {}, None
    try:
        if file:
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS:
                raise DomainError("unsupported_media", "支持mp4/webm/wav/m4a/mp3/ogg", 422)
            path = core.data_dir / "media" / f"{uuid.uuid4().hex}{suffix}"
            sha = hashlib.sha256()
            size = 0
            with path.open("xb") as stream:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise DomainError("upload_too_large", "上传不得超过100MB", 413)
                    sha.update(chunk)
                    stream.write(chunk)
            if not size:
                raise DomainError("empty_media", "媒体文件为空", 422)
            from interview_media import inspect_media
            try:
                meta = inspect_media(path)
            except Exception as error:
                raise DomainError("invalid_media", f"媒体未通过完整性检查：{error}", 422) from error
            if not meta.get("has_audio") and not meta.get("has_video"):
                raise DomainError("invalid_media", "文件不含有效音视频", 422)
            if not 0 < meta.get("duration_ms", 0) <= 180000:
                raise DomainError("invalid_duration", "音视频必须大于0秒且不超过3分钟", 422)
            media_hash = sha.hexdigest()
        result = core.submit_answer(session_id, round_id, attempt_id, idempotency_key, transcript, path, meta, media_hash, language, replace_answer_id)
        # Repeated uploads may have produced a new temporary path; only retain the committed file.
        if path:
            committed, _ = core.media_path(session_id, result["answer_id"])
            if committed != path:
                path.unlink(missing_ok=True)
        return result
    except BaseException:
        if path:
            path.unlink(missing_ok=True)
        raise
    finally:
        if file:
            file.file.close()


@app.post("/v1/interviews/{session_id}/answers/{answer_id}/transcript")
def revise(session_id: str, answer_id: str, body: RevisionBody, role=Depends(authorize)):
    return core.revise_transcript(session_id, answer_id, actor=role, **body.model_dump())


@app.get('/v1/interviews/{session_id}/answers/{answer_id}/timeline')
def answer_timeline(session_id: str, answer_id: str,
                    transcript_revision: Annotated[int, Query(ge=1)], role=Depends(hr_role)):
    return core.get_answer_timeline(session_id, answer_id, transcript_revision)



@app.get('/v1/interviews/{session_id}/report.pdf')
def report_pdf(session_id: str, version: int = Query(..., ge=1), role=Depends(hr_role)):
    # Explicit version prevents an export click from silently switching reports.
    from interview_pdf import build_report_pdf, PdfExportError
    frozen = core.get_report(session_id, version)
    try:
        content = build_report_pdf(frozen)
    except PdfExportError as error:
        raise DomainError('pdf_unavailable', str(error), 503) from None
    return Response(content, media_type='application/pdf', headers={
        'Content-Disposition': 'attachment; filename="zhenjian-interview-v' + str(version) + '.pdf"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'X-Report-Version': str(version)})

@app.get("/v1/interviews/{session_id}/report")
def report(session_id: str, version: int | None = None, compact: bool = False, role=Depends(hr_role)):
    result = core.get_report(session_id, version)
    if compact:
        from interview_core import compact_report
        return compact_report(result)
    return result


@app.post("/v1/interviews/{session_id}/reviews")
def review(session_id: str, body: ReviewBody, role=Depends(hr_role)):
    return core.submit_review(session_id, **body.model_dump())


@app.post("/v1/interviews/{session_id}/followup-assessments")
def assessment(session_id: str, body: AssessmentBody, role=Depends(hr_role)):
    return core.assess_followup(session_id, **body.model_dump())


@app.get("/v1/interviews/{session_id}/media/{answer_id}")
def media(session_id: str, answer_id: str, role=Depends(authorize)):
    path, media_type = core.media_path(session_id, answer_id)
    return FileResponse(path, media_type=media_type)


@app.get('/v1/interviews/{session_id}/visual/{answer_id}')
def visual_quality(session_id: str, answer_id: str, role=Depends(hr_role)):
    from interview_visual import visual_quality as inspect_visual
    path, _ = core.media_path(session_id, answer_id)
    result = inspect_visual(path, core.data_dir)
    return {**result, 'answer_id': answer_id,
            'frames': [{**frame, 'frame_url': f'/v1/interviews/{session_id}/visual/{answer_id}/frames/{frame["index"]}'}
                       for frame in result['frames']]}


@app.get('/v1/interviews/{session_id}/visual/{answer_id}/frames/{index}')
def visual_frame(session_id: str, answer_id: str, index: int, role=Depends(hr_role)):
    from interview_visual import VisualError, visual_frame as cached_frame
    path, _ = core.media_path(session_id, answer_id)
    try:
        frame = cached_frame(path, core.data_dir, index)
    except (VisualError, OSError):
        raise DomainError('not_found', '缩略帧不存在；请先打开此回答的画面质量记录', 404) from None
    return FileResponse(frame, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
