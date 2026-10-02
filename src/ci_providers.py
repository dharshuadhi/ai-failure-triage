"""Live CI provider: triage real failures from GitHub Actions.

Pulls failed workflow runs from any public repo (no auth needed for reads),
downloads the job logs, and converts them into Failure objects — the same
pipeline as local files, but on live data.
"""

import io
import json
import re
import urllib.request
import zipfile

from .models import Failure
from .parsers import parse_generic_log, parse_pytest_text

API = "https://api.github.com"


def _first_error_line(text: str) -> str:
    """Best-effort one-line summary: first line that looks like an error."""
    generic = re.compile(r"process completed with exit code|##\[error\]exit", re.IGNORECASE)
    fallback = ""
    for line in text.splitlines():
        s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+", "", line).strip()
        if not re.search(r"error|failed|exception|ERR!", s, re.IGNORECASE):
            continue
        if len(s) >= 200:
            continue
        if generic.search(s):
            fallback = fallback or s
            continue
        return s
    return fallback


def _get(url, token=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ai-failure-triage",
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _download(url, token=None):
    """Download bytes, stripping the Authorization header on cross-host redirects.

    GitHub log URLs 302-redirect to blob storage, which rejects GitHub tokens.
    """
    class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new_req and urllib.request.urlparse(newurl).netloc != urllib.request.urlparse(req.full_url).netloc:
                new_req.remove_header("Authorization")
            return new_req

    opener = urllib.request.build_opener(NoAuthRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "ai-failure-triage",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with opener.open(req, timeout=120) as resp:
        return resp.read()


class GitHubActionsProvider:
    """Fetch failures from a repo's GitHub Actions runs."""

    def __init__(self, repo, token=None):
        self.repo = repo          # "owner/name"
        self.token = token

    def failed_runs(self, limit=5):
        data = _get(f"{API}/repos/{self.repo}/actions/runs"
                    f"?status=failure&per_page={limit}", self.token)
        return data.get("workflow_runs", [])

    def failed_jobs(self, run_id):
        data = _get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/jobs",
                    self.token)
        return [j for j in data.get("jobs", [])
                if j.get("conclusion") == "failure"]

    def job_log_text(self, job_id) -> str:
        """Download a job's log archive and return concatenated text."""
        raw = _download(f"{API}/repos/{self.repo}/actions/jobs/{job_id}/logs",
                        self.token)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                texts = []
                for name in zf.namelist():
                    if name.endswith(".txt"):
                        texts.append(f"===== {name} =====\n" +
                                     zf.read(name).decode("utf-8", "replace"))
                return "\n".join(texts)
        except zipfile.BadZipFile:
            return raw.decode("utf-8", "replace")

    def collect_failures(self, limit=3, max_jobs=3) -> list:
        """End-to-end: failed runs -> failed jobs -> parsed Failures."""
        failures = []
        for run in self.failed_runs(limit):
            run_id = run["id"]
            for job in self.failed_jobs(run_id)[:max_jobs]:
                try:
                    log = self.job_log_text(job["id"])
                except Exception as exc:  # noqa: BLE001 - one bad log shouldn't kill the run
                    failures.append(Failure(
                        test_id=f"{self.repo}#{run_id}/{job['name']}",
                        message=f"log download failed: {exc}",
                        source=f"{self.repo} run {run_id}"))
                    continue
                parsed = parse_pytest_text(log) or parse_generic_log(log)
                if parsed:
                    for f in parsed:
                        f.source = f"{self.repo} run {run_id} / {job['name']}"
                    failures.extend(parsed)
                else:
                    # no parseable test failures — keep the tail of the log,
                    # where the actual error usually surfaces
                    tail = "\n".join(log.splitlines()[-60:])
                    failures.append(Failure(
                        test_id=f"{self.repo}#{run_id}/{job['name']}",
                        message=_first_error_line(tail) or (job.get("conclusion") or "failed"),
                        traceback=tail[-2000:],
                        source=f"{self.repo} run {run_id}"))
        return failures
