"""Additive intent workspace routes; existing middleware owns authentication/CSRF."""
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .intent_workspace import IntentError, IntentWorkspace


class CompileBody(BaseModel):
    raw_request: str = Field(min_length=1, max_length=20000)
    interpretation: dict | None = None


class ReviseBody(BaseModel):
    expected_revision: int = Field(ge=1)
    interpretation: dict


class RevisionBody(BaseModel):
    expected_revision: int = Field(ge=1)


def router_for(store, owner, before_create, authorize):
    service = IntentWorkspace(store)
    router = APIRouter()
    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except IntentError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.get('/workspace')
    def page():
        return FileResponse(Path(__file__).parent/'static/workspace.html')

    @router.get('/api/v1/intents')
    def listing():
        return service.list(owner())

    @router.post('/api/v1/intents', status_code=201)
    def compile(body: CompileBody):
        return call(service.compile, owner(), body.raw_request, body.interpretation)

    @router.get('/api/v1/intents/{id}')
    def get(id: str):
        return call(service.get, owner(), id)

    @router.patch('/api/v1/intents/{id}')
    def revise(id: str, body: ReviseBody):
        return call(service.revise, owner(), id, body.expected_revision, body.interpretation)

    @router.post('/api/v1/intents/{id}/cancel')
    def cancel(id: str, body: RevisionBody):
        return call(service.cancel, owner(), id, body.expected_revision)

    @router.post('/api/v1/intents/{id}/confirm')
    def confirm(id: str, body: RevisionBody):
        return call(service.confirm, owner(), id, body.expected_revision, before_create=lambda: before_create(owner()))

    @router.post('/api/v1/intents/{id}/authorize')
    def allow(id: str, body: RevisionBody):
        intent = call(service.get, owner(), id)
        if intent['revision'] != body.expected_revision or not intent['mission_id']:
            raise HTTPException(409, '请先确认当前Intent版本')
        return authorize(intent['mission_id'])

    return router
