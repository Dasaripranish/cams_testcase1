"""
Rate limiting middleware with per-tenant limits.
Implements sliding window algorithm without external dependencies.
"""

import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from cams_otel_lib import Logger as logger, otel_trace



@otel_trace
def _track_request(method: str, path: str, status_code: int, duration: float, tenant_id: str) -> None:
    """Forward request metrics to Prometheus if available, otherwise no-op."""
    if _metrics_collector is not None:
        try:
            _metrics_collector.track_request(method, path, status_code, duration, tenant_id)
        except Exception:
            pass


@dataclass
class RateLimitConfig:
    """Rate limit configuration."""

    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_size: int = 10  # Allow burst of requests
    enabled: bool = True


class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter with per-tenant tracking.

    Features:
    - Per-tenant rate limiting
    - Multiple time windows (minute, hour)
    - Burst capacity
    - In-memory tracking (no external dependencies)
    - Thread-safe
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        burst_size: int = 10,
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Max requests per minute per tenant
            requests_per_hour: Max requests per hour per tenant
            burst_size: Allow burst of N requests
        """
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.burst_size = burst_size

        # Track requests per tenant: {tenant_id: deque[(timestamp, 1)]}
        self._minute_windows: Dict[str, deque] = defaultdict(deque)
        self._hour_windows: Dict[str, deque] = defaultdict(deque)

        # Lock for thread safety
        self._lock = Lock()

        logger.info(
            "Rate limiter initialized",
            requests_per_minute=requests_per_minute,
            requests_per_hour=requests_per_hour,
            burst_size=burst_size,
        )

    @otel_trace
    def _cleanup_window(self, window: deque, window_seconds: int, current_time: float):
        """Remove expired entries from window."""
        cutoff_time = current_time - window_seconds
        while window and window[0] < cutoff_time:
            window.popleft()

    @otel_trace
    def check_rate_limit(self, tenant_id: str) -> Tuple[bool, Dict[str, int]]:
        """
        Check if request is within rate limits.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Tuple of (allowed: bool, rate_limit_info: dict)
        """
        current_time = time.time()

        with self._lock:
            # Get windows for this tenant
            minute_window = self._minute_windows[tenant_id]
            hour_window = self._hour_windows[tenant_id]

            # Cleanup old entries
            self._cleanup_window(minute_window, 60, current_time)
            self._cleanup_window(hour_window, 3600, current_time)

            # Count current requests
            minute_count = len(minute_window)
            hour_count = len(hour_window)

            # Check limits
            minute_allowed = minute_count < self.requests_per_minute
            hour_allowed = hour_count < self.requests_per_hour

            # Allow burst if both windows have capacity
            # burst_allowed = minute_count < (self.requests_per_minute + self.burst_size)

            allowed = minute_allowed and hour_allowed

            # If allowed, record the request
            if allowed:
                minute_window.append(current_time)
                hour_window.append(current_time)

            # Calculate remaining and reset times
            minute_remaining = max(0, self.requests_per_minute - minute_count)
            hour_remaining = max(0, self.requests_per_hour - hour_count)

            # Calculate when limits reset
            minute_reset = (
                int(current_time + 60) if minute_window else int(current_time)
            )
            hour_reset = int(current_time + 3600) if hour_window else int(current_time)

            rate_limit_info = {
                "limit_minute": self.requests_per_minute,
                "remaining_minute": minute_remaining,
                "reset_minute": minute_reset,
                "limit_hour": self.requests_per_hour,
                "remaining_hour": hour_remaining,
                "reset_hour": hour_reset,
                "retry_after": (
                    60 if not minute_allowed else (3600 if not hour_allowed else 0)
                ),
            }

            if not allowed:
                logger.warning(
                    "Rate limit exceeded",
                    tenant_id=tenant_id,
                    minute_count=minute_count,
                    hour_count=hour_count,
                    limit_minute=self.requests_per_minute,
                    limit_hour=self.requests_per_hour,
                )

            return allowed, rate_limit_info

    @otel_trace
    def get_stats(self, tenant_id: str) -> Dict[str, int]:
        """
        Get current stats for a tenant.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Dict with current request counts
        """
        current_time = time.time()

        with self._lock:
            minute_window = self._minute_windows[tenant_id]
            hour_window = self._hour_windows[tenant_id]

            self._cleanup_window(minute_window, 60, current_time)
            self._cleanup_window(hour_window, 3600, current_time)

            return {
                "requests_last_minute": len(minute_window),
                "requests_last_hour": len(hour_window),
                "limit_minute": self.requests_per_minute,
                "limit_hour": self.requests_per_hour,
            }

    @otel_trace
    def reset_tenant(self, tenant_id: str):
        """
        Reset rate limits for a tenant.

        Args:
            tenant_id: Tenant identifier
        """
        with self._lock:
            if tenant_id in self._minute_windows:
                del self._minute_windows[tenant_id]
            if tenant_id in self._hour_windows:
                del self._hour_windows[tenant_id]

            logger.info("Rate limits reset", tenant_id=tenant_id)

    @otel_trace
    def get_all_stats(self) -> Dict[str, Dict[str, int]]:
        """
        Get stats for all tenants.

        Returns:
            Dict mapping tenant_id to stats
        """
        stats = {}
        current_time = time.time()

        # Inline the per-tenant logic here to avoid re-acquiring the non-reentrant lock
        # (calling self.get_stats() inside with self._lock: would deadlock).
        with self._lock:
            all_tenants = set(self._minute_windows.keys()) | set(
                self._hour_windows.keys()
            )

            for tenant_id in all_tenants:
                minute_window = self._minute_windows[tenant_id]
                hour_window = self._hour_windows[tenant_id]
                self._cleanup_window(minute_window, 60, current_time)
                self._cleanup_window(hour_window, 3600, current_time)
                stats[tenant_id] = {
                    "requests_last_minute": len(minute_window),
                    "requests_last_hour": len(hour_window),
                    "limit_minute": self.requests_per_minute,
                    "limit_hour": self.requests_per_hour,
                }

        return stats


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for rate limiting.

    Features:
    - Per-tenant rate limiting
    - Rate limit headers (X-RateLimit-*)
    - Configurable limits
    - Health check exemption
    """

    def __init__(
        self,
        app,
        rate_limiter: SlidingWindowRateLimiter,
        enabled: bool = True,
        exempt_paths: Optional[List[str]] = None,
    ):
        """
        Initialize middleware.

        Args:
            app: FastAPI application
            rate_limiter: Rate limiter instance
            enabled: Enable/disable rate limiting
            exempt_paths: Paths to exempt from rate limiting
        """
        super().__init__(app)
        self.rate_limiter = rate_limiter
        self.enabled = enabled
        self.exempt_paths = exempt_paths or [
            "/health",
            "/health/ready",
            "/health/live",
            "/.well-known/agent-card.json",
        ]

        logger.info(
            "Rate limiting middleware initialized",
            enabled=enabled,
            exempt_paths=self.exempt_paths,
        )

    @otel_trace
    def _extract_tenant_id(self, request: Request) -> str:
        """Extract tenant UUID from JWT claims set by ClaimsMiddleware."""
        claims = getattr(request.state, "claims", None)
        if claims:
            return claims.get("tenant_uuid", "unknown")
        return "unknown"

    async def dispatch(self, request: Request, call_next):
        """Process request with rate limiting."""
        import time

        start_time = time.time()
        status_code = 200

        # Skip if disabled
        if not self.enabled:
            response = await call_next(request)
            duration = time.time() - start_time
            tenant_id = self._extract_tenant_id(request)
            _track_request(request.method, request.url.path, response.status_code, duration, tenant_id)
            return response

        # Skip exempt paths
        if request.url.path in self.exempt_paths:
            response = await call_next(request)
            duration = time.time() - start_time
            tenant_id = self._extract_tenant_id(request)
            _track_request(request.method, request.url.path, response.status_code, duration, tenant_id)
            return response

        # Extract tenant ID
        tenant_id = self._extract_tenant_id(request)

        # Check rate limit
        allowed, rate_limit_info = self.rate_limiter.check_rate_limit(tenant_id)

        # If not allowed, return 429
        if not allowed:
            status_code = 429
            logger.warning(
                "Request rate limited",
                tenant_id=tenant_id,
                path=request.url.path,
                method=request.method,
            )

            # Track rate limited request
            duration = time.time() - start_time
            _track_request(request.method, request.url.path, status_code, duration, tenant_id)

            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "message": f"Too many requests. Try again in {rate_limit_info['retry_after']} seconds.",
                    "retry_after": rate_limit_info["retry_after"],
                },
                headers={
                    "X-RateLimit-Limit-Minute": str(rate_limit_info["limit_minute"]),
                    "X-RateLimit-Remaining-Minute": str(
                        rate_limit_info["remaining_minute"]
                    ),
                    "X-RateLimit-Reset-Minute": str(rate_limit_info["reset_minute"]),
                    "X-RateLimit-Limit-Hour": str(rate_limit_info["limit_hour"]),
                    "X-RateLimit-Remaining-Hour": str(
                        rate_limit_info["remaining_hour"]
                    ),
                    "X-RateLimit-Reset-Hour": str(rate_limit_info["reset_hour"]),
                    "Retry-After": str(rate_limit_info["retry_after"]),
                },
            )

        # Process request
        response = await call_next(request)

        # Track request metrics
        duration = time.time() - start_time
        _track_request(request.method, request.url.path, response.status_code, duration, tenant_id)

        # Add rate limit headers to response
        response.headers["X-RateLimit-Limit-Minute"] = str(
            rate_limit_info["limit_minute"]
        )
        response.headers["X-RateLimit-Remaining-Minute"] = str(
            rate_limit_info["remaining_minute"]
        )
        response.headers["X-RateLimit-Reset-Minute"] = str(
            rate_limit_info["reset_minute"]
        )
        response.headers["X-RateLimit-Limit-Hour"] = str(rate_limit_info["limit_hour"])
        response.headers["X-RateLimit-Remaining-Hour"] = str(
            rate_limit_info["remaining_hour"]
        )
        response.headers["X-RateLimit-Reset-Hour"] = str(rate_limit_info["reset_hour"])

        return response


# Global rate limiter instance
_rate_limiter: Optional[SlidingWindowRateLimiter] = None


@otel_trace
def get_rate_limiter() -> SlidingWindowRateLimiter:
    """
    Get global rate limiter instance.

    Returns:
        SlidingWindowRateLimiter instance
    """
    global _rate_limiter

    if _rate_limiter is None:
        from src.config.settings import settings

        requests_per_minute = getattr(settings, "rate_limit_per_minute", 60)
        requests_per_hour = getattr(settings, "rate_limit_per_hour", 1000)
        burst_size = getattr(settings, "rate_limit_burst_size", 10)

        _rate_limiter = SlidingWindowRateLimiter(
            requests_per_minute=requests_per_minute,
            requests_per_hour=requests_per_hour,
            burst_size=burst_size,
        )

    return _rate_limiter
