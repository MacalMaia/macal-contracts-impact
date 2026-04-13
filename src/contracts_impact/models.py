from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class HttpProvider(BaseModel):
    method: HttpMethod
    path: str
    handler: str
    line: int


class HttpConsumer(BaseModel):
    target: str
    method: HttpMethod
    path: str
    caller: str
    line: int


class TopicPublished(BaseModel):
    topic: str
    event_schema: str | None = Field(default=None, alias="schema")
    publisher: str
    line: int

    model_config = ConfigDict(populate_by_name=True)


class TopicSubscribed(BaseModel):
    topic: str
    handler: str | None = None
    line: int | None = None
    push_endpoint: str | None = None
    dlq: str | None = None


class EventSchemaRef(BaseModel):
    class_name: str
    file: str
    line: int


class Provides(BaseModel):
    http: list[HttpProvider] = Field(default_factory=list)
    topics_published: list[TopicPublished] = Field(default_factory=list)


class Consumes(BaseModel):
    http: list[HttpConsumer] = Field(default_factory=list)
    topics_subscribed: list[TopicSubscribed] = Field(default_factory=list)


class ExtractionWarning(BaseModel):
    kind: str
    file: str
    line: int
    message: str


class ServiceContracts(BaseModel):
    service: str
    extractor_version: str = "0.1.0"
    provides: Provides = Field(default_factory=Provides)
    consumes: Consumes = Field(default_factory=Consumes)
    event_schemas: list[EventSchemaRef] = Field(default_factory=list)
    extraction_warnings: list[ExtractionWarning] = Field(
        default_factory=list, alias="_extraction_warnings"
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @classmethod
    def new(cls, service: str, repo_path: Path) -> ServiceContracts:
        # repo_path is accepted for API compat but no longer stored — git history
        # tracks when the file changed and the file path identifies the service.
        return cls(service=service)
