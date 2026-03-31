# Resume Matcher API (Frontend Integration)

This API is intended for non-Streamlit frontends (React/Next/Vue/etc.).

Base URL (docker-compose default): `http://<host>:8000`

## Endpoints

### `GET /health`
Health check.

### `POST /jobs`
Submit a matching job.

Form-data fields:
- `jd_mode`: `"upload"` or `"paste"`
- `jd_text`: required when `jd_mode=paste`
- `jd_file`: required when `jd_mode=upload`
- `resumes`: one or more resume files

Returns:
```json
{
  "job_id": "uuid",
  "status": "queued",
  "run_output_dir": "/app/outputs/sessions/..."
}
```

### `POST /match-resumes`
Single-call endpoint for other teams:
- Input: same as `POST /jobs` (JD + resumes as form-data)
- Behavior: waits for completion and returns a ZIP with edited recruiter PDFs
- Timeout: configurable via form field `wait_timeout_seconds` (default 600)

Responses:
- `200` + `application/zip` (`edited_resumes.zip`) when finished in time
- `202` JSON with `job_id` when still processing (caller can poll async endpoints)
- `500` when worker job fails

### `GET /jobs/{job_id}`
Poll status.

Returns:
- `status`: `queued | started | finished | failed`
- `last_heartbeat` when available
- `result.count` and `result.error_count` when finished
- `error` when failed

### `GET /jobs/{job_id}/results`
Get completed structured outputs (after `status=finished`).

Returns:
- `summary_rows`
- file names (`csv`, `summary_json`, `pdfs`)

### `GET /jobs/{job_id}/files/{filename}`
Download a result file (CSV/JSON/PDF) for that job.

---

## Polling flow (recommended)

1. Submit job (`POST /jobs`) and store `job_id`.
2. Poll `GET /jobs/{job_id}` every 2-5 seconds.
3. On `finished`, call `GET /jobs/{job_id}/results`.
4. Use `/jobs/{job_id}/files/{filename}` for downloads.

