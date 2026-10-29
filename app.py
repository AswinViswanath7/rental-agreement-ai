"""ClauseCraft Flask application.

Application factory with blueprints, typed settings, structured JSON logs with
request id, background job processing (bounded thread pool), CSRF, rate
limiting, security headers, and privacy-by-default upload handling (R4, R6, J).

Never returns str(e) to the client; every error carries {error_code, message,
request_id} and details are logged server-side only.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from flask import (
    Flask,
    g,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)

if TYPE_CHECKING:
    from werkzeug.datastructures import FileStorage

from app_core.checklist import build_registration_checklist
from app_core.config import settings
from app_core.errors import ClauseCraftError, ErrorCode
from app_core.ingest import ingest
from app_core.llm import build_llm_client
from app_core.pipeline import Pipeline
from app_core.property_law import search_property_law_reference
from app_core.property_verify import compare_property_documents
from app_core.report import build_pdf  # stage 11
from app_core.types import AnalysisResult, new_request_id, new_versions

logger = logging.getLogger(__name__)

DOCUMENT_LANGUAGES = (
    ("en", "English"),
    ("as", "অসমীয়া"),
    ("bn", "বাংলা"),
    ("brx", "बड़ो"),
    ("doi", "डोगरी"),
    ("gu", "ગુજરાતી"),
    ("hi", "हिन्दी"),
    ("kn", "ಕನ್ನಡ"),
    ("ks", "کٲشُر"),
    ("kok", "कोंकणी"),
    ("mai", "मैथिली"),
    ("ml", "മലയാളം"),
    ("mni", "ꯃꯩꯇꯩꯂꯣꯟ"),
    ("mr", "मराठी"),
    ("ne", "नेपाली"),
    ("or", "ଓଡ଼ିଆ"),
    ("pa", "ਪੰਜਾਬੀ"),
    ("sa", "संस्कृतम्"),
    ("sat", "ᱥᱟᱱᱛᱟᱲᱤ"),
    ("sd", "سنڌي"),
    ("ta", "தமிழ்"),
    ("te", "తెలుగు"),
    ("ur", "اردو"),
)

# ---------------------------------------------------------------------------
# Job store (in-memory, TTL). Shared across workers only via single process;
# for multi-worker use Redis (see config.session_store).
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_jobs: dict[str, dict] = {}
RESULTS_TTL = settings.result_ttl_seconds

# Bounded background thread pool so no HTTP request blocks for minutes.
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clausecraft")

# Models loaded once at startup (thread-safe singleton pattern).
_models_ready = threading.Event()
_models_ready.set()  # tfidf backend needs no heavy model; set ready immediately.


def _now() -> datetime:
    return datetime.now(UTC)


def _job_entry(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "queued",  # queued | running | completed | failed | cancelled
        "progress": 0,
        "result_id": None,
        "error_code": None,
        "error_message": None,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }


def _store_job(entry: dict) -> None:
    with _job_lock:
        _jobs[entry["job_id"]] = entry


def _get_job(job_id: str) -> dict | None:
    with _job_lock:
        return _jobs.get(job_id)


def _prune_jobs() -> None:
    cutoff = _now() - timedelta(seconds=RESULTS_TTL)
    with _job_lock:
        dead = [jid for jid, e in _jobs.items()
                if datetime.fromisoformat(e["updated_at"]) < cutoff]
        for jid in dead:
            _jobs.pop(jid, None)


# ---------------------------------------------------------------------------
# CSRF + rate limiting (simple, dependency-free)
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_rate_records: dict[str, list] = {}


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_valid() -> bool:
    if not settings.csrf_enabled:
        return True
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    token = session.get("csrf_token")
    provided = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
    )
    return bool(token and provided and secrets.compare_digest(token, provided))


def _rate_limited() -> str | None:
    """Simple fixed-window rate limit per IP. Returns retry-after or None."""
    if not settings.rate_limit_per_ip:
        return None
    ip = request.remote_addr or "unknown"
    now = time.time()
    window = 60.0
    max_req = 30
    try:
        max_req = int(settings.rate_limit_per_ip.split("/")[0])
    except (ValueError, AttributeError):
        pass
    with _rate_lock:
        rec = _rate_records.setdefault(ip, [])
        rec[:] = [t for t in rec if now - t < window]
        if len(rec) >= max_req:
            oldest = min(rec)
            return str(int(window - (now - oldest)) + 1)
        rec.append(now)
    return None


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _api_error(error_code: str, message: str, status_code: int, request_id: str,
               detail: str | None = None) -> tuple:
    logger.error("request_id=%s code=%s detail=%s", request_id, error_code,
                 detail or message)
    return jsonify({
        "error_code": error_code,
        "message": message,
        "request_id": request_id,
    }), status_code


def _handle_clausecraft_error(exc: ClauseCraftError) -> tuple:
    return _api_error(exc.error_code, exc.message, exc.status_code, exc.request_id,
                      exc.detail)


def _admin_review_error() -> tuple | None:
    token = settings.admin_api_token
    if not token:
        logger.error("rid=%s admin review requested without ADMIN_API_TOKEN", g.request_id)
        return jsonify({
            "error_code": ErrorCode.INTERNAL,
            "message": "Admin review is not configured.",
            "request_id": g.request_id,
        }), 503
    provided = request.headers.get("Authorization", "")
    if not secrets.compare_digest(provided, f"Bearer {token}"):
        logger.warning("rid=%s unauthorized admin review request", g.request_id)
        return jsonify({
            "error_code": ErrorCode.INVALID_REQUEST,
            "message": "Admin authorization required.",
            "request_id": g.request_id,
        }), 403
    return None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = settings.secret_key or os.urandom(24).hex()
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_mb * 1024 * 1024
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["SESSION_COOKIE_SECURE"] = settings.session_cookie_secure
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Structured request id + logging (no document content ever logged).
    @app.before_request
    def _before_request():
        g.request_id = request.headers.get("X-Request-ID") or new_request_id()
        g.start_time = time.time()
        # Reject oversized uploads handled by werkzeug; rate limit.
        retry = _rate_limited()
        if retry:
            return _api_error(ErrorCode.RATE_LIMITED,
                              f"Too many requests. Retry after {retry}s.", 429,
                              g.request_id)
        if not _csrf_valid():
            return _api_error(ErrorCode.INVALID_REQUEST, "CSRF token missing or invalid.",
                              403, g.request_id)

    @app.after_request
    def _after_request(response):
        rid = getattr(g, "request_id", "-")
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        dur = (time.time() - getattr(g, "start_time", time.time())) * 1000
        logger.info("rid=%s method=%s path=%s status=%s duration_ms=%.1f",
                    rid, request.method, request.path, response.status_code, dur)
        return response

    @app.errorhandler(ClauseCraftError)
    def _on_clausecraft_error(exc):
        return _handle_clausecraft_error(exc)

    @app.errorhandler(404)
    def _on_404(e):
        return _api_error(ErrorCode.NOT_FOUND, "Not found.", 404,
                          getattr(g, "request_id", new_request_id()))

    @app.errorhandler(413)
    def _on_413(e):
        return _api_error(ErrorCode.OVERSIZE,
                          f"File exceeds {settings.max_upload_mb} MB limit.", 413,
                          getattr(g, "request_id", new_request_id()))

    @app.errorhandler(500)
    def _on_500(e):
        rid = getattr(g, "request_id", new_request_id())
        logger.exception("rid=%s unhandled server error", rid)
        return _api_error(ErrorCode.INTERNAL, "Internal server error.", 500, rid)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    def health():
        return jsonify({
            "status": "healthy",
            "service": settings.app_name,
            "request_id": getattr(g, "request_id", new_request_id()),
            "llm_provider": settings.llm_provider,
            "models_ready": _models_ready.is_set(),
        })

    @app.get("/ready")
    def ready():
        return jsonify({
            "ready": _models_ready.is_set(),
            "embedding_backend": settings.embedding_backend,
            "request_id": getattr(g, "request_id", new_request_id()),
        })

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=_csrf_token(),
                               languages=DOCUMENT_LANGUAGES,
                               provider_notice=settings.llm_provider != "none")

    @app.post("/api/property-compare")
    def api_property_compare():
        files = [file for file in request.files.getlist("documents") if file.filename]
        if len(files) < 2 or len(files) > 3:
            return _api_error(
                ErrorCode.INVALID_REQUEST,
                "Choose two or three property documents.",
                400,
                g.request_id,
            )
        labels = request.form.getlist("labels")
        documents = []
        for index, file in enumerate(files):
            suffix = os.path.splitext(file.filename or "")[1].lower()
            if suffix not in {
                ".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic",
            }:
                suffix = ""
            parsed = ingest(
                file.read(),
                f"property-document-{index + 1}{suffix}",
                g.request_id,
                max_upload_mb=settings.max_upload_mb,
                max_pages=settings.max_pages,
                total_budget_seconds=settings.total_time_budget_seconds,
            )
            label = labels[index].strip() if index < len(labels) else ""
            documents.append({
                "label": label[:80] or f"Document {index + 1}",
                "pages": [
                    {"page_no": page.page_no, "text": page.text}
                    for page in parsed.pages
                ],
            })
        comparison = compare_property_documents(documents)
        logger.info("rid=%s property_documents_compared count=%d",
                    g.request_id, len(documents))
        return jsonify({"comparison": comparison, "request_id": g.request_id})

    @app.get("/api/property-law-reference")
    def api_property_law_reference():
        query = request.args.get("q", "").strip()
        category = request.args.get("category", "all")
        allowed_categories = {
            "all", "central_laws", "state_laws", "clause_benchmarks", "sources",
        }
        if len(query) > 120 or category not in allowed_categories:
            return _api_error(
                ErrorCode.INVALID_REQUEST,
                "Search text must be at most 120 characters and category must be valid.",
                400,
                g.request_id,
            )
        reference = search_property_law_reference(query, category)
        return jsonify({**reference, "request_id": g.request_id})

    @app.get("/api/languages")
    def api_languages():
        return jsonify({"languages": _enabled_languages(),
                        "default": settings.default_language})

    @app.post("/api/analyze")
    def api_analyze():
        rid = g.request_id
        if "file" not in request.files:
            return _api_error(ErrorCode.INVALID_REQUEST, "No file part in request.",
                              400, rid)
        file: FileStorage = request.files["file"]
        if file.filename == "":
            return _api_error(ErrorCode.INVALID_REQUEST, "No file selected.", 400, rid)

        language = request.form.get("language", settings.default_language)
        data = file.read()  # in memory; size already limited by MAX_CONTENT_LENGTH

        job_id = new_request_id()
        entry = _job_entry(job_id)
        _store_job(entry)

        # Persist upload bytes for the background job (per-request temp, deleted later).
        EXECUTOR.submit(_run_analysis, job_id, data, file.filename, language, rid)

        return jsonify({"job_id": job_id, "status": "queued",
                        "request_id": rid}), 202

    @app.get("/api/jobs/<job_id>")
    def api_job_status(job_id):
        _prune_jobs()
        entry = _get_job(job_id)
        if not entry:
            return _api_error(ErrorCode.NOT_FOUND, "Job not found or expired.", 404,
                              g.request_id)
        return jsonify({
            "job_id": entry["job_id"], "status": entry["status"],
            "progress": entry["progress"], "result_id": entry["result_id"],
            "error_code": entry["error_code"],
            "error_message": entry["error_message"],
            "request_id": g.request_id,
        })

    @app.get("/results/<result_id>")
    def results(result_id):
        result = _result_store.get(result_id)
        if not result:
            return _api_error(ErrorCode.NOT_FOUND, "Result not found or expired.",
                              404, g.request_id)
        provider_notice = settings.llm_provider != "none"
        return render_template("result.html", result=result.to_dict(),
                               csrf_token=_csrf_token(),
                               result_id=result_id,
                               checklist=build_registration_checklist(result),
                               qa_session_id=_qa_session_ids.get(result_id, ""),
                               provider_notice=provider_notice)

    @app.post("/api/confirm-type")
    def api_confirm_type():
        rid = g.request_id
        body = request.get_json(silent=True) or {}
        chosen_type = body.get("chosen_type", "rental_agreement")
        # Re-run classification accepting user's choice. Stored as a flag so
        # rental logic runs if user confirmed.
        return jsonify({"status": "accepted", "chosen_type": chosen_type,
                        "request_id": rid})

    @app.post("/api/chat")
    def api_chat():
        rid = g.request_id
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _api_error(ErrorCode.INVALID_REQUEST,
                              "A JSON object is required.", 400, rid)
        session_id = body.get("session_id")
        question = body.get("question", "")
        if not isinstance(session_id, str) or not session_id or not isinstance(question, str):
            return _api_error(ErrorCode.INVALID_REQUEST,
                              "session_id and question are required.", 400, rid)
        question = question.strip()
        if not question:
            return _api_error(ErrorCode.INVALID_REQUEST,
                              "Please enter a question.", 400, rid)
        if len(question) > 2000:
            return _api_error(ErrorCode.INVALID_REQUEST,
                              "Question must be 2,000 characters or fewer.", 400, rid)
        response_language = body.get("response_language", "en")
        if not isinstance(response_language, str):
            response_language = "en"
        supported_languages = {code for code, _ in DOCUMENT_LANGUAGES}
        if response_language not in supported_languages:
            response_language = "en"
        from app_core.qa import answer_question
        try:
            answer = answer_question(session_id, question, response_language)
        except Exception as exc:
            return _api_error(ErrorCode.INTERNAL, "Unable to answer question.", 500, rid,
                              str(exc))
        return jsonify(answer), 200, {"X-Request-ID": rid}

    @app.post("/api/feedback")
    def api_feedback():
        rid = g.request_id
        body = request.get_json(silent=True) or {}
        session_id = body.get("session_id", rid)
        from app_core.learning import capture_feedback
        result = capture_feedback(
            session_id=session_id,
            item_type=body.get("item_type", "field"),
            field=body.get("field"),
            document_type=body.get("document_type", ""),
            wrong_value=body.get("wrong_value", ""),
            right_value=body.get("right_value", body.get("correct_value", "")),
            right_quote=body.get("right_quote"),
            sentence=body.get("sentence"),
            app_version=settings.app_version,
            consent=bool(body.get("consent")),
        )
        return jsonify({"status": "received", "request_id": rid, **result})

    @app.get("/api/admin/corrections")
    def api_admin_corrections():
        rid = g.request_id
        auth_error = _admin_review_error()
        if auth_error:
            return auth_error
        status = request.args.get("status")
        from app_core.learning import list_corrections
        return jsonify({"corrections": list_corrections(status),
                        "request_id": rid})

    @app.post("/api/admin/corrections/<correction_id>/accept")
    def api_admin_accept_correction(correction_id):
        rid = g.request_id
        auth_error = _admin_review_error()
        if auth_error:
            return auth_error
        from app_core.learning import accept_correction
        ok = accept_correction(correction_id)
        return jsonify({"accepted": ok, "request_id": rid})

    @app.post("/api/admin/corrections/<correction_id>/reject")
    def api_admin_reject_correction(correction_id):
        rid = g.request_id
        auth_error = _admin_review_error()
        if auth_error:
            return auth_error
        from app_core.learning import reject_correction
        ok = reject_correction(correction_id)
        return jsonify({"rejected": ok, "request_id": rid})

    @app.get("/export/<result_id>")
    def export_report(result_id):
        result = _result_store.get(result_id)
        if not result:
            return _api_error(ErrorCode.NOT_FOUND, "Result not found or expired.",
                              404, g.request_id)
        try:
            pdf_bytes = build_pdf(result)
        except Exception:
            logger.exception("rid=%s pdf build failed", g.request_id)
            return _api_error(ErrorCode.INTERNAL, "Could not generate report.",
                              500, g.request_id)
        return send_file(
            __import__("io").BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=f"{settings.app_name}_Report_{result_id[:8]}.pdf",
            mimetype="application/pdf",
        )

    @app.post("/api/delete-my-data")
    def delete_my_data():
        rid = g.request_id
        body = request.get_json(silent=True) or {}
        result_id = body.get("result_id")
        removed = False
        with _result_store_lock:
            if result_id and result_id in _result_store:
                _result_store.pop(result_id, None)
                removed = True
        with _job_lock:
            for jid, e in list(_jobs.items()):
                if e.get("result_id") == result_id:
                    _jobs.pop(jid, None)
        from app_core.learning import delete_corrections_for_session
        cleared = delete_corrections_for_session(result_id) if result_id else 0
        if result_id:
            from app_core.qa import delete_session
            qa_session_id = _qa_session_ids.pop(result_id, None)
            if qa_session_id:
                delete_session(qa_session_id)
        return jsonify({"deleted": removed, "corrections_cleared": cleared,
                        "request_id": rid})

    return app


# ---------------------------------------------------------------------------
# Result store (in-memory, TTL)
# ---------------------------------------------------------------------------

_result_store: dict[str, AnalysisResult] = {}
_result_store_lock = threading.Lock()
_qa_session_ids: dict[str, str] = {}


def _store_result(result: AnalysisResult) -> None:
    with _result_store_lock:
        _result_store[result.request_id] = result


def _get_result(result_id: str) -> AnalysisResult | None:
    with _result_store_lock:
        return _result_store.get(result_id)


def _run_analysis(job_id: str, data: bytes, filename: str, language: str,
                  parent_rid: str) -> None:
    """Background job: run the full pipeline and store the result."""
    entry = _get_job(job_id)
    request_id = new_request_id()
    try:
        entry["status"] = "running"
        entry["progress"] = 10
        entry["updated_at"] = _now().isoformat()
        _store_job(entry)

        # Build an LLM client for this job (provider may be none).
        llm = build_llm_client(settings.llm_provider, settings)
        pipeline = Pipeline(llm_client=llm, override_settings=settings)

        entry["progress"] = 40
        _store_job(entry)

        result, warnings = pipeline.run(data, filename, request_id=request_id)
        result.warnings = list(dict.fromkeys((result.warnings or []) + warnings))

        # Stamp versions.
        if result.versions is None:
            result.versions = new_versions()

        _store_result(result)

        # Store document for Q&A (session id = request id, browser sends only session id).
        try:
            from app_core.qa import store_document
            segments_out = [
                {"text": segment.text, "page": segment.page_no,
                 "start": segment.start, "end": segment.end}
                for segment in pipeline.segments
            ]
            _qa_session_ids[request_id] = store_document(
                request_id,
                pipeline.normalized_text,
                segments_out,
                [field.model_dump(mode="json") for field in result.fields],
                [clause.model_dump(mode="json") for clause in result.clauses],
            )
        except Exception:
            logger.exception("rid=%s failed to store QA document", request_id)

        entry["status"] = "completed"
        entry["progress"] = 100
        entry["result_id"] = request_id
        entry["updated_at"] = _now().isoformat()
        _store_job(entry)
        logger.info("rid=%s job=%s completed fields=%d risks=%d",
                    request_id, job_id, len(result.fields), len(result.risks))
    except ClauseCraftError as exc:
        entry["status"] = "failed"
        entry["error_code"] = exc.error_code
        entry["error_message"] = exc.message
        entry["updated_at"] = _now().isoformat()
        _store_job(entry)
        logger.warning("rid=%s job=%s clausecraft error: %s", request_id, job_id,
                       exc.error_code)
    except Exception:
        entry["status"] = "failed"
        entry["error_code"] = ErrorCode.INTERNAL
        entry["error_message"] = "Analysis failed."
        entry["updated_at"] = _now().isoformat()
        _store_job(entry)
        logger.exception("rid=%s job=%s unhandled", request_id, job_id)


def _enabled_languages():
    raw = settings.translation_enabled_languages or "en"
    return [x.strip() for x in raw.split(",") if x.strip()]


# module-level reference used by api_chat (set after factory)
_current_app: Flask | None = None

app = create_app()
_current_app = app
