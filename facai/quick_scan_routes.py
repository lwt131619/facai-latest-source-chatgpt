"""Login-free fast assessment API: recon fans out, attack agent auto-fires.

Public by design. Abuse is bounded by per-IP/per-host rate gates plus the scan
concurrency cap. `run_attack` defaults to true: a host that raises a concrete
signal is attacked in the same request, so the caller gets one report that
already contains confirmed injection results instead of a to-do list.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field

from .quick_scan import QuickScans
from .site_assessment import SiteAssessmentError


class ScanRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    attack: bool = True


def _client_ip(request):
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
            result = service.scan(body.url, _client_ip(request), run_attack=body.attack)
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
