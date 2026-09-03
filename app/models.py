from typing import Literal

from pydantic import BaseModel, Field

RecordType = Literal["A", "AAAA"]


class CheckRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=253)
    type: RecordType = "A"


class Answer(BaseModel):
    name: str
    type: str  # record type mapped to text (A / AAAA / CNAME / ...)
    ttl: int | None = None
    data: str


class RegionResult(BaseModel):
    region: str  # the locationHint requested (wnam, weur, ...)
    colo: str | None = None  # the Cloudflare colo that actually ran the lookup
    status: int | None = None  # DNS RCODE (0 = NOERROR, 2 = SERVFAIL, 3 = NXDOMAIN)
    answers: list[Answer] = []
    cnameChain: list[str] = []
    latencyMs: int | None = None
    error: str | None = None


class CheckResponse(BaseModel):
    name: str
    type: RecordType
    results: list[RegionResult]
