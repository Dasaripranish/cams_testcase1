# Selected Features

This agent template has been customized with the following features:

### Enterprise

**Multi-Tenant Support**
- Tenant-specific configurations and resource isolation
- Per-tenant rate limiting and metrics tracking
- Tenant ID propagation through all logs and traces

### Observability

**Langfuse Observability**
- Automatic tracing of all agent executions and LLM calls
- Real-time token usage and cost tracking per request
- Visual trace timeline with prompts, responses, and metadata

**LiteLLM Gateway**
- Centralized LLM request routing through LiteLLM proxy
- Track all your API calls, costs, and usage in a native control tower
- Unified observability across multiple LLM providers (OpenAI, Anthropic, Google)
- Built-in caching, load balancing, and fallback support

### Production

**Rate Limiting**
- Per-tenant sliding window rate limits (60/min, 1000/hour)
- Burst capacity for handling traffic spikes
- Rate limit headers and stats endpoint at /stats/rate-limits

### Storage

**PostgreSQL Database**
- Persistent conversation memory across sessions
- LangGraph checkpoint storage for state persistence
- Automatic fallback to in-memory if not configured

### Ux

**Streaming Support**
- Real-time response streaming for better UX
- Token-by-token output delivery
- Configurable via STREAMING_ENABLED flag

## Core Features (Always Included)

- LangGraph Workflow Engine
- A2A Protocol Support
- Multi-LLM Provider Support (OpenAI, Anthropic, Google)
- Structured JSON Logging
- Health Check Endpoints
- Tool Auto-Discovery System
- LiteLLM Gateway Integration

---

**For detailed documentation, see:**
- `README.md` - Complete user guide with deployment workflow
- `docs/COMPLETE_GUIDE.md` - Deep technical documentation
