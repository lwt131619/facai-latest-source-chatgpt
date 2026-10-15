"""Owner-scoped Trust v1 API, separate from the legacy execution proof API."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .mission_spec import MissionSpecifications
from .trust import TrustError, TrustStore


class Revision(BaseModel):
    expected_revision: int = Field(ge=0)


class EvidenceBody(Revision):
    content: str = Field(default='', max_length=20000)
    source: str = Field(default='', max_length=2000)
    event_id: str | None = Field(default=None, max_length=100)


class ClaimBody(Revision):
    statement: str = Field(min_length=1, max_length=2000)
    subject: str = Field(min_length=1, max_length=2000)
    predicate: str = Field(min_length=1, max_length=2000)
    value: str = Field(min_length=1, max_length=2000)
    context: dict[str, str] = Field(default_factory=dict, max_length=4)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewBody(Revision):
    verdict: str
    reason: str = Field(min_length=1, max_length=2000)


class AcceptBody(BaseModel):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    reason: str = Field(min_length=1, max_length=2000)


def router_for(store, owner, require_mission, human_writer):
    specifications = MissionSpecifications(store)
    service = TrustStore(store)
    router = APIRouter()

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except TrustError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.get('/evidence-center')
    def page():
        return FileResponse(Path(__file__).parent/'static/evidence-center.html')

    @router.get('/api/v1/missions/{mission_id}/specification')
    def spec(mission_id: str):
        require_mission(mission_id, allow_assist=False)
        try:
            result = specifications.get(owner(), mission_id)
        except ValueError:
            raise HTTPException(409, 'Mission specification integrity check failed') from None
        if result is None:
            raise HTTPException(404, 'Legacy mission has no confirmed specification')
        return result

    @router.get('/api/v1/missions/{mission_id}/trust')
    def get(mission_id: str):
        return call(service.get, owner(), mission_id)

    @router.post('/api/v1/missions/{mission_id}/trust/evidence', status_code=201)
    def evidence(mission_id: str, body: EvidenceBody):
        return call(service.add_evidence, owner(), mission_id, body.expected_revision,
                    content=body.content, source=body.source, event_id=body.event_id)

    @router.post('/api/v1/missions/{mission_id}/trust/claims', status_code=201)
    def claim(mission_id: str, body: ClaimBody):
        return call(service.add_claim, owner(), mission_id, body.expected_revision,
                    **body.model_dump(exclude={'expected_revision'}))

    @router.post('/api/v1/missions/{mission_id}/trust/claims/{claim_id}/reviews', status_code=201)
    def review(mission_id: str, claim_id: str, body: ReviewBody, request: Request):
        human_writer(request)
        return call(service.review, owner(), mission_id, body.expected_revision, claim_id,
                    verdict=body.verdict, reason=body.reason)

    @router.post('/api/v1/missions/{mission_id}/trust/proofs', status_code=201)
    def proof(mission_id: str, body: Revision):
        return call(service.snapshot, owner(), mission_id, body.expected_revision)

    @router.post('/api/v1/missions/{mission_id}/trust/proofs/{proof_id}/accept')
    def accept(mission_id: str, proof_id: str, body: AcceptBody, request: Request):
        human_writer(request)
        return call(service.accept, owner(), mission_id, proof_id, body.sha256, body.reason)

    @router.get('/api/v1/missions/{mission_id}/trust/proofs/{proof_id}/download')
    def download(mission_id: str, proof_id: str):
        content = call(service.export, owner(), mission_id, proof_id)
        return Response(content, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="facai-proof.zip"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    return router
