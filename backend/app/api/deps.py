"""FastAPI dependencies: service container access and bearer-token authentication."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.services.container import Services


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def require_token(request: Request, services: Annotated[Services, Depends(get_services)]) -> None:
    expected = request.app.state.token
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied.strip(), expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


def get_db(services: Annotated[Services, Depends(get_services)]) -> Iterator[Session]:
    session = services.session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


Svc = Annotated[Services, Depends(get_services)]
Db = Annotated[Session, Depends(get_db)]
Authed = Depends(require_token)
