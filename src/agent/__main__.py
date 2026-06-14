"""
Agent server entry point.
Run with: python -m src.agent
"""

import asyncio
import json
import os
import sys

import uvicorn
from contextlib import asynccontextmanager
from fastapi import Depends, Request, Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.apps import A2AFastAPIApplication
from a2a.types import AgentCard, AgentCapabilities, AgentProvider, AgentSkill

from pathlib import Path

from .executor import AgentExecutor
from src.config.settings import settings
from src.middleware.claims_middleware import ClaimsMiddleware
from src.auth_config.platform_auth import rbac_dependency, auth_db
from src.utils.health import get_health_checker
from cams_otel_lib import Logger as logger, Otel_Client, otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


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


try:
    from src.middleware.rate_limiting import RateLimitMiddleware, get_rate_limiter
    RATE_LIMITING_AVAILABLE = True
except ImportError:
    RateLimitMiddleware = None
    get_rate_limiter = None
    RATE_LIMITING_AVAILABLE = False





@otel_trace
def create_agent_card() -> AgentCard:
    """Build the A2A agent card from agent_config.json agent_definition."""
    from src.config.cams_config_adapter import CAMSConfigAdapter

    # Load raw CAMS config to access agent_definition fields
    agent_def = {}
    capabilities_cfg = {}
    provider_cfg = {}
    skills_cfg = []
    raw_version = "1.0.0"
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            import json as _json
            raw = _json.loads(cfg_path.read_text())
            adapter = CAMSConfigAdapter(raw)
            agent_def = adapter.get_agent_definition()
            capabilities_cfg = agent_def.get("capabilities", {})
            provider_cfg = agent_def.get("provider", {})
            skills_cfg = agent_def.get("skills", [])
            raw_version = agent_def.get("version", "1.0.0")
    except Exception as e:
        logger.warning(f"Could not load agent_definition from config, using defaults: {e}")

    # Build skills — use config skills if defined, else keep template defaults
    if skills_cfg:
        skills = [
            AgentSkill(
                id=s.get("id", f"skill_{i}"),
                name=s.get("name", f"Skill {i}"),
                description=s.get("description", ""),
                tags=s.get("tags", []),
                examples=s.get("examples", []),
                input_modes=s.get("inputModes", ["text/plain"]),
                output_modes=s.get("outputModes", ["text/plain"]),
            )
            for i, s in enumerate(skills_cfg)
        ]
    else:
        skills = [
            AgentSkill(
                id="search_and_answer",
                name="Search & Answer",
                description="Search knowledge base and provide detailed answers",
                tags=["search", "qa", "knowledge"],
                examples=["What is machine learning?", "Explain quantum computing concepts"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            ),
        ]

    return AgentCard(
        name=agent_def.get("name") or settings.agent_name,
        description=agent_def.get("description") or settings.agent_description,
        url=settings.agent_url,
        version=raw_version,
        protocol_version="0.3.0",
        preferred_transport="HTTP+JSON",
        default_input_modes=agent_def.get("defaultInputModes", ["text/plain"]),
        default_output_modes=agent_def.get("defaultOutputModes", ["text/plain"]),
        capabilities=AgentCapabilities(
            streaming=capabilities_cfg.get("streaming", settings.streaming_enabled),
            push_notifications=capabilities_cfg.get("push_notifications", False),
        ),
        skills=skills,
        supports_authenticated_extended_card=False,
        provider=AgentProvider(
            organization=provider_cfg.get("organization", "CAMS"),
            url=provider_cfg.get("url", ""),
        ),
        documentation_url=None,
        icon_url=None,
    )


async def main():
    """Main entry point for the agent server."""
    # Initialize OTEL client first — must happen before any logging or tracing
    Otel_Client.initialize_otel_client(
        service_name=settings.agent_name,
        environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
        agent_id=_read_agent_instance_id(),
    )

    # Instrument outgoing HTTP calls (requests + httpx) as early as possible
    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    logger.info(f"Starting agent: {settings.agent_name} on {settings.host}:{settings.port}")

    # Create agent card
    agent_card = create_agent_card()

    # Create agent executor
    executor = AgentExecutor()

    # Create request handler with in-memory task store
    task_store = InMemoryTaskStore()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    # Create A2A FastAPI application
    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    # Build the FastAPI app
    app = a2a_app.build()

    # Instrument incoming FastAPI requests
    FastAPIInstrumentor().instrument_app(app)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await auth_db.initialize()
            logger.info("Auth database initialized")
        except Exception as e:
            logger.error(f"Auth database initialization failed: {e}")
            raise
        yield

    app.router.lifespan_context = lifespan

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://aifabric-frontend.dev.cams.covasant.io",
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:8000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,  # Required for Keycloak auth
        allow_methods=["*"],
        allow_headers=["*"],     # Allows Authorization headers
    )

    if RATE_LIMITING_AVAILABLE and settings.rate_limit_enabled:
        rate_limiter = get_rate_limiter()
        app.add_middleware(
            RateLimitMiddleware,
            rate_limiter=rate_limiter,
            enabled=settings.rate_limit_enabled,
        )
        logger.info(f"Rate limiting enabled: {settings.rate_limit_per_minute}/min, {settings.rate_limit_per_hour}/hr")

    # ClaimsMiddleware must be added LAST so it runs FIRST (Starlette LIFO order).
    # It decodes the JWT and sets request.state.claims before any other middleware reads it.
    app.add_middleware(ClaimsMiddleware)

    # Get health checker
    health_checker = get_health_checker()

    # Add enhanced health check endpoints

    @app.get("/health")
    async def health_check():
        """
        Detailed health check with component status.
        Returns overall health and individual component statuses.
        """
        health_status = await health_checker.get_health_status()

        # Return 503 if unhealthy, 200 otherwise
        status_code = 503 if health_status["status"] == "unhealthy" else 200

        from fastapi import Response

        return Response(
            content=json.dumps(health_status),
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/health/ready")
    async def readiness_check():
        """
        Kubernetes readiness probe.
        Returns 200 if ready to serve traffic, 503 if not ready.
        """
        is_ready = await health_checker.is_ready()

        if is_ready:
            return {"status": "ready"}
        else:
            from fastapi import Response

            return Response(
                content='{"status": "not ready"}',
                status_code=503,
                media_type="application/json",
            )

    @app.get("/health/live")
    async def liveness_check():
        """
        Kubernetes liveness probe.
        Returns 200 if application is alive.
        """
        is_alive = await health_checker.is_alive()

        if is_alive:
            return {"status": "alive"}
        else:
            from fastapi import Response

            return Response(
                content='{"status": "not alive"}',
                status_code=503,
                media_type="application/json",
            )

    if RATE_LIMITING_AVAILABLE:
        @app.get("/stats/rate-limits", dependencies=[Depends(rbac_dependency)])
        async def rate_limit_stats():
            """
            Get rate limit statistics for all tenants.
            Useful for monitoring and debugging.
            """
            if not settings.rate_limit_enabled:
                return {"enabled": False}

            rate_limiter = get_rate_limiter()
            stats = rate_limiter.get_all_stats()

            return {
                "enabled": True,
                "config": {
                    "per_minute": settings.rate_limit_per_minute,
                    "per_hour": settings.rate_limit_per_hour,
                    "burst_size": settings.rate_limit_burst_size,
                },
                "tenants": stats,
            }


    # Add unified agent endpoint - same payload as normal agents
    @app.post("/agent/run", dependencies=[Depends(rbac_dependency)])
    async def agent_run_endpoint(http_request: Request, body: dict = Body(...)):
        """
        Direct agent execution endpoint.

        Request format:
        {
            "query": "Your question here",
            "app_id": "optional-app-id",
            "conversation_id": "optional-conversation-id",
            "user_id": "optional-user-id"
        }

        Response format:
        {
            "response": "Agent's answer",
            "app_id": "...",
            "conversation_id": "...",
            "user_id": "..."
        }
        """
        try:
            import uuid
            from langchain_core.messages import HumanMessage

            # Extract request fields
            query = body.get("query", "")
            if not query:
                return {"error": "Query is required"}

            # Tenant UUID comes exclusively from JWT claims set by ClaimsMiddleware
            tenant_id = http_request.state.claims.get("tenant_uuid")
            if not tenant_id:
                return {"error": "tenant_uuid not found in token claims"}
            thread_id = body.get("conversation_id") or f"thread_{uuid.uuid4().hex[:16]}"

            logger.info(f"Agent request received: query_length={len(query)}, conversation_id={thread_id}")

            # Generate trace context for Langfuse
            trace_id = uuid.uuid4().hex
            parent_span_id = uuid.uuid4().hex[:16]

            # Build agent state
            initial_state = {
                "messages": [HumanMessage(content=query)],
                "search_query": "",
                "retrieved_context": "",
                "final_response": "",
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "needs_retrieval": True,
                "_langfuse_trace_id": trace_id,
                "_langfuse_span_id": parent_span_id,
                "litellm_headers": None,
            }

            # Execute agent — reuse _run_graph so checkpointing and thread config apply here too
            final_state = await executor._run_graph(initial_state, tenant_id, thread_id)

            response_text = final_state.get("final_response", "No response generated")

            logger.info(f"Agent response generated: response_length={len(response_text)}, conversation_id={thread_id}")

            return {
                "response": response_text,
                "app_id": body.get("app_id", ""),
                "conversation_id": body.get("conversation_id", ""),
                "user_id": body.get("user_id", "")
            }

        except Exception as e:
            logger.error(f"Agent error: {str(e)}")
            return {"error": str(e)}

    # Run server
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )

    server = uvicorn.Server(config)

    logger.info(f"Agent ready at http://{settings.host}:{settings.port}")

    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
        sys.exit(0)
