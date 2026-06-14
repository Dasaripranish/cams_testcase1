"""
Observability Module — Langfuse Integration

This file is ONLY present when the 'langfuse' feature is selected.
It provides tracing and observability for agent execution using Langfuse.

Supports:
- Trace ID and parent span ID from incoming requests
- Automatic ID generation when not provided
- Span management for agent execution
- Generation tracking for LLM calls
"""

import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

from cams_otel_lib import Logger as logger, otel_trace

# ---------------------------------------------------------------------------
# Guard the Langfuse SDK import so the module never hard-crashes on load
# even if the package is somehow missing.
# ---------------------------------------------------------------------------
try:
    from langfuse import Langfuse
    from langfuse.types import TraceContext
    _LANGFUSE_AVAILABLE = True
except ImportError:
    Langfuse = None  # type: ignore[assignment,misc]
    TraceContext = None  # type: ignore[assignment,misc]
    _LANGFUSE_AVAILABLE = False
    logger.warning(
        "langfuse package not installed — observability is disabled. "
        "Add 'langfuse>=2.44.0' to requirements.txt to enable tracing."
    )

try:
    from a2a.server.agent_execution import RequestContext
except ImportError:
    RequestContext = None  # type: ignore[assignment,misc]


class ObservabilityManager:
    """
    Manages observability and tracing for agent execution.

    Credentials come from agent_config.json → secrets.langfuse:
        {
            "enabled": true,
            "public_key": "pk-lf-...",
            "secret_key": "sk-lf-...",
            "host": "https://cloud.langfuse.com"
        }

    When disabled or misconfigured every method is a no-op and context
    managers yield None, so calling code never needs to branch on
    ``observability.enabled``.
    """

    def __init__(self, langfuse_config: Optional[Dict[str, Any]] = None):
        langfuse_config = langfuse_config or {}

        self.enabled: bool = langfuse_config.get("enabled", False)
        self.public_key: str = langfuse_config.get("public_key", "")
        self.secret_key: str = langfuse_config.get("secret_key", "")
        self.base_url: str = langfuse_config.get("host", "https://cloud.langfuse.com")
        self.client: Optional[Any] = None

        if not _LANGFUSE_AVAILABLE:
            self.enabled = False
            logger.debug("ObservabilityManager: langfuse SDK not available, tracing disabled")
            return

        if self.enabled and self._is_configured():
            self._initialize_client()
        else:
            logger.info("Langfuse observability disabled or credentials not configured")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @otel_trace
    def _is_configured(self) -> bool:
        if not all([self.public_key, self.secret_key, self.base_url]):
            logger.warning("Langfuse credentials missing in config.secrets.langfuse")
            return False
        return True

    @otel_trace
    def _initialize_client(self) -> None:
        try:
            self.client = Langfuse(
                public_key=self.public_key,
                secret_key=self.secret_key,
                base_url=self.base_url,
            )
            logger.info(f"Langfuse client initialised (host: {self.base_url})")
        except Exception as e:
            logger.warning(f"Failed to initialise Langfuse client: {e}")
            self.enabled = False
            self.client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @otel_trace
    def extract_trace_context(
        self, context: Any
    ) -> tuple:
        """Extract (trace_id, parent_span_id) from A2A request context."""
        try:
            metadata = getattr(context, "metadata", None)
            if metadata:
                trace_id = metadata.get("langfuse_trace_id")
                parent_span_id = metadata.get("langfuse_parent_span_id")
                if trace_id and parent_span_id:
                    return trace_id, parent_span_id
        except Exception as e:
            logger.debug(f"Could not extract trace context: {e}")
        return None, None

    @otel_trace
    def create_trace_context(
        self, context: Any, generate_if_missing: bool = True
    ) -> Optional[Any]:
        """Return a Langfuse TraceContext, generating IDs if needed."""
        if not self.enabled or TraceContext is None:
            return None

        trace_id, parent_span_id = self.extract_trace_context(context)

        if generate_if_missing:
            if not trace_id:
                trace_id = uuid.uuid4().hex
            if not parent_span_id:
                parent_span_id = uuid.uuid4().hex[:16]

        if trace_id and parent_span_id:
            return TraceContext(trace_id=trace_id, parent_span_id=parent_span_id)
        return None

    @contextmanager
    def trace_agent_execution(
        self,
        context: Any,
        agent_name: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager that wraps the full agent execution in a Langfuse span.

        Yields the span object (or None when tracing is disabled).
        Usage::

            with obs.trace_agent_execution(ctx, "my-agent", query) as span:
                result = execute()
                if span:
                    span.update(output=result)
        """
        if not self.enabled or not self.client:
            yield None
            return

        trace_context = self.create_trace_context(context, generate_if_missing=True)
        if not trace_context:
            yield None
            return

        agent_span = None
        try:
            agent_span = self.client.start_span(
                trace_context=trace_context,
                name=f"agent.{agent_name}",
                input={"query": user_input},
                metadata=metadata or {},
            )
            yield agent_span
        except Exception as e:
            if agent_span:
                try:
                    agent_span.update(level="ERROR", status_message=str(e))
                except Exception:
                    pass
            raise
        finally:
            if agent_span:
                try:
                    agent_span.end()
                except Exception:
                    pass
            if self.client:
                try:
                    self.client.flush()
                except Exception:
                    pass

    @contextmanager
    def trace_generation(
        self,
        parent_span: Any,
        name: str,
        input_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Context manager for tracing a single LLM generation within a span."""
        if not self.enabled or not parent_span:
            yield None
            return

        generation = None
        try:
            generation = parent_span.start_observation(
                as_type="generation",
                name=name,
                input=input_data,
                metadata=metadata or {},
            )
            yield generation
        finally:
            if generation:
                try:
                    generation.end()
                except Exception:
                    pass

    @otel_trace
    def flush(self) -> None:
        """Flush pending traces to Langfuse."""
        if self.client:
            try:
                self.client.flush()
            except Exception as e:
                logger.warning(f"Error flushing Langfuse client: {e}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_observability_manager: Optional[ObservabilityManager] = None


@otel_trace
def set_observability_manager(manager: ObservabilityManager) -> None:
    """Register a pre-built ObservabilityManager as the global instance."""
    global _observability_manager
    _observability_manager = manager
    logger.debug("Global observability manager set")


@otel_trace
def get_observability_manager(
    langfuse_config: Optional[Dict[str, Any]] = None,
) -> ObservabilityManager:
    """Return the global ObservabilityManager, creating one if needed."""
    global _observability_manager
    if _observability_manager is None:
        _observability_manager = ObservabilityManager(langfuse_config=langfuse_config or {})
    return _observability_manager
