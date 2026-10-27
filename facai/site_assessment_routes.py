"""Authenticated, owner-scoped site-control and assessment API."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .site_assessment import SiteAssessmentError, SiteAssessments


class SiteClaim(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


def router_for(store, owner, human_writer):
    service = SiteAssessments(store)
    router = APIRouter()

    def call(fn, *args):
        try:
            return fn(*args)
        except SiteAssessmentError as exc:
            raise HTTPException(400 if str(exc) != 'Site claim not found' else 404, str(exc)) from None

    @router.post('/api/v1/site-assessments', status_code=201)
    def create(body: SiteClaim, request: Request):
        human_writer(request)
        return call(service.create, owner(), body.url)

    @router.post('/api/v1/site-assessments/{claim_id}/verify')
    def verify(claim_id: str, request: Request):
        human_writer(request)
        return call(service.verify, owner(), claim_id)

    @router.post('/api/v1/site-assessments/{claim_id}/run')
    def run(claim_id: str, request: Request):
        human_writer(request)
        return call(service.assess, owner(), claim_id)

    @router.get('/api/v1/site-assessments/{claim_id}')
    def get(claim_id: str):
        return call(service.get, owner(), claim_id)

    return router
