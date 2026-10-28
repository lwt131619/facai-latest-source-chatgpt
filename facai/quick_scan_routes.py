"""Login-free quick scan API: paste a URL, get an evidence-backed report.

Public by design (Owner directive 2026-10-28); abuse is bounded by the
per-IP and per-host rate gates inside QuickScans plus the scan concurrency
cap, not by accounts. The report contains only what the target site itself
publishes to any anonymous visitor.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field

from .quick_scan import QuickScans, SiteAssessmentError


class ScanRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


def _client_ip(request):
    # Behind the deployment nginx, X-Forwarded-For is "client, ...proxies" and
    # the LAST entry is the hop our own nginx appended, i.e. the real client.
    # Direct-to-uvicorn calls have no header and use the socket address.
    forwarded = request.headers.get('x-forwarded-for', '')
    if forwarded:
        return forwarded.split(',')[-1].strip() or (request.client.host if request.client else 'unknown')
    return request.client.host if request.client else 'unknown'


def router_for(store):
    service = QuickScans(store)
    router = APIRouter()

    @router.get('/scan')
    def page():
        return FileResponse(Path(__file__).parent / 'static/scan.html')

    @router.post('/api/v1/quick-scans', status_code=200)
    def scan(body: ScanRequest, request: Request):
        try:
            result = service.scan(body.url, _client_ip(request))
        except SiteAssessmentError as exc:
            raise HTTPException(400, str(exc)) from None
        if result['status'] == 'RATE_LIMITED':
            raise HTTPException(429, f'扫描过于频繁，请 {result["retry_after"]} 秒后再试')
        if result['status'] == 'BUSY':
            raise HTTPException(429, '当前扫描队列繁忙，请稍后再试')
        return {'id': result.get('id'), 'cached': result['cached'], 'report': result['report']}

    @router.get('/api/v1/quick-scans/{scan_id}')
    def get(scan_id: str):
        report = service.get(scan_id)
        if report is None:
            raise HTTPException(404, 'Report not found')
        return {'report': report}

    return router
