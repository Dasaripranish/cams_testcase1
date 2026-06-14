"""
Claims Middleware — JWT verification, claim extraction, and OTEL context initialization.

Verifies the JWT from the Authorization header, sets request.state.claims so that
downstream rbac_dependency can authorize the request, and wires up the OTEL
RequestContext (user_id, tenant, scope, app, agent_id) so every log line and span
produced during the request carries those attributes automatically.

Context vars (set_request_context / set_observability_client) are reset after each
request so context never leaks between concurrent async requests.
"""

import os
import uuid
import json
from pathlib import Path

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from cams_otel_lib import (
    Logger as logger,
    Otel_Client,
    otel_trace,
    RequestContext,
    set_request_context,
    set_observability_client,
    reset_request_context,
    reset_observability_client,
)
from src.config.settings import settings
from src.auth_config.platform_auth import auth_config

# Paths that skip JWT verification (health probes, API docs)
_SKIP_PATHS = {"/health", "/health/live", "/health/ready", "/docs", "/openapi.json", "/redoc"}


def _read_agent_instance_id() -> str:
    """Read agent_instance_id from config. APP_CONFIG_PATH takes priority (CAMS multi-instance)."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        if ext:
            p = Path(ext)
            if p.exists():
                with open(p) as f:
                    return json.load(f).get("agent_instance_id", "N/A")
        config_path = Path(__file__).parent.parent / "config" / "agent_config.json"
        with open(config_path) as f:
            return json.load(f).get("agent_instance_id", "N/A")
    except Exception:
        return "N/A"


_AGENT_INSTANCE_ID = _read_agent_instance_id()


class ClaimsMiddleware(BaseHTTPMiddleware):
    """
    Middleware that verifies the JWT, extracts claims, and wires up OTEL request context.

    On every non-health request:
    1. Verifies the JWT from Authorization: Bearer <token> using the configured signing key.
    2. Sets request.state.claims so rbac_dependency can authorize downstream routes.
    3. Stores user_id, tenant, scope, app in RequestContext for automatic OTEL attribution.
    4. Re-initialises Otel_Client with agent identity (no-op when OTEL_CONFIG_UUID unchanged).
    5. Resets context vars after the response to prevent context bleed across async requests.
    """

    @otel_trace
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        claims = {}
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                claims = jwt.decode(
                    token,
                    auth_config.jwt_signing_key,
                    algorithms=[auth_config.jwt_algorithm.value],
                    audience=auth_config.jwt_audience,
                    issuer=auth_config.jwt_issuer,
                )
            except Exception as e:
                logger.warning(f"JWT verification failed: {e}")

        # Set claims on request.state — rbac_dependency reads this
        request.state.claims = claims

        # Wire up OTEL RequestContext with extracted claims (context-var based, per-request)
        request_context_token = None
        otel_client_token = None
        try:
            request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
            tenant_name = claims.get("tenant_uuid", "N/A")
            scope_name = claims.get("scope", "N/A")
            userid = claims.get("user_uuid", "N/A")
            app_name = claims.get("app_name", "N/A")
            session_id = claims.get("session_id", uuid.uuid4().hex.lower())

            request_context = RequestContext(
                request_id=request_id,
                tenant_name=tenant_name,
                scope_name=scope_name,
                userid=userid,
                app_name=app_name,
                agent_id=_AGENT_INSTANCE_ID,
                session_id=session_id,
                service_name=settings.agent_name,
            )
            request_context_token = set_request_context(request_context)

            otel_client = Otel_Client.initialize_otel_client(
                service_name=settings.agent_name,
                environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
                agent_id=_AGENT_INSTANCE_ID,
            )
            otel_client_token = set_observability_client(otel_client)
        except Exception as e:
            logger.error(f"Error setting up OTEL request context: {e}")

        response = await call_next(request)

        # Reset context vars so they don't bleed into the next request on this coroutine
        if request_context_token:
            reset_request_context(request_context_token)
        if otel_client_token:
            reset_observability_client(otel_client_token)

        return response
