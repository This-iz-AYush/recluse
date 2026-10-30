"""
webdash/security.py — Constant-time safe comparison, IP normalization,
right-most XFF & trusted origin header resolution, dual-pool bounded in-memory
rate limiting, CSRF protection, and security headers.
"""

from __future__ import annotations

import collections
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import time
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("recluse.webdash.security")

_MISSING_ORIGIN_SECRET_WARNED = False
_REQUIRE_WITHOUT_ORIGIN_SECRET_ERRORED = False

# Typed request keys (avoids Python 3.14 / aiohttp dict-mutation & NotAppKeyWarning)
_RequestKey = getattr(web, "RequestKey", web.AppKey)
CLIENT_IP_KEY = _RequestKey("client_ip", str)
CLIENT_IP_TRUSTED_KEY = _RequestKey("client_ip_trusted", bool)
CLIENT_IP_SOURCE_KEY = _RequestKey("client_ip_source", str)
SESSION_DATA_KEY = _RequestKey("session_data", dict)

SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# secrets.token_urlsafe(32) always produces a 43-char URL-safe base64 string
PLAUSIBLE_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
    "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com data:; "
    "img-src 'self' https://cdn.discordapp.com https://media.discordapp.net https://ui-avatars.com data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def is_plausible_session_token(token: str | None) -> bool:
    """
    Cheap regex pre-check for `recluse_sid` cookies.
    Returns True iff `token` is a 43-character URL-safe base64 string
    matching `secrets.token_urlsafe(32)` (`^[A-Za-z0-9_-]{43}$`).
    """
    if not token or not isinstance(token, str) or len(token) != 43:
        return False
    return bool(PLAUSIBLE_SESSION_TOKEN_RE.match(token))


def is_secure_request(request: web.Request) -> bool:
    """Return True when X-Forwarded-Proto is https or request.secure is True."""
    xf_proto = request.headers.get("X-Forwarded-Proto", "")
    if xf_proto:
        first_proto = xf_proto.split(",")[0].strip().lower()
        if first_proto == "https":
            return True
    return bool(getattr(request, "secure", False))


def safe_compare(a: str | bytes | None, b: str | bytes | None) -> bool:
    """
    Constant-time comparison that safely handles arbitrary UTF-8 strings,
    non-ASCII characters (e.g. 'é'), and surrogate-escaped header bytes
    (e.g. '\\udcff') without ever raising TypeError or UnicodeError.
    """
    if a is None or b is None:
        return False
    if not isinstance(a, (str, bytes)) or not isinstance(b, (str, bytes)):
        return False
    try:
        a_bytes = a.encode("utf-8", errors="surrogateescape") if isinstance(a, str) else bytes(a)
        b_bytes = b.encode("utf-8", errors="surrogateescape") if isinstance(b, str) else bytes(b)
        if not a_bytes or not b_bytes:
            return False
        return secrets.compare_digest(a_bytes, b_bytes)
    except (TypeError, UnicodeError, Exception):
        return False


def normalize_ip(value: str | None) -> str:
    """
    Validate and normalize an IPv4 or IPv6 address string.
    Rejects values containing '%', whitespace, or non-ASCII characters.
    Unwraps IPv4-mapped IPv6 addresses (e.g., ::ffff:1.2.3.4 -> 1.2.3.4).
    Returns "invalid" on any invalid input.
    """
    if not isinstance(value, str) or not value:
        return "invalid"
    if "%" in value or any(c.isspace() for c in value) or not value.isascii():
        return "invalid"
    try:
        addr = ipaddress.ip_address(value)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        return "invalid"


def _parse_trusted_hops(override: int | None = None) -> int:
    if override is not None:
        return max(0, int(override))
    raw = os.getenv("TRUSTED_PROXY_HOPS", "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def resolve_client_ip_details(
    request: web.Request,
    *,
    trusted_hops: int | None = None,
    origin_secret: str | None = None,
) -> tuple[str, bool, str]:
    """
    Resolve the client IP and return (normalized_ip, is_trusted_source, source_label).

    Rules:
    1. Trusted-Header Mode (ORIGIN_SHARED_SECRET):
       - If ORIGIN_SHARED_SECRET is configured (via arg or env) and the request
         includes X-Recluse-Origin-Secret matching it via safe_compare,
         use X-Recluse-Client-IP (normalized). If valid, return (ip, True, "origin_header").
       - Otherwise X-Recluse-Client-IP is completely ignored.
       - If ORIGIN_SHARED_SECRET is configured on the server, but the request is
         missing or fails X-Recluse-Origin-Secret, the request bypassed the Cloudflare
         Worker and hit Render directly; only 1 proxy hop from the right (Render's own
         load balancer) is trusted in that fallback path.
    2. Right-Most X-Forwarded-For Resolution:
       - Default TRUSTED_PROXY_HOPS = 1.
       - Never falls back to hops[0] (left-most entry is client-spoofable).
       - If len(hops) >= effective_hops, pick hops[-effective_hops].
       - If 0 < len(hops) < effective_hops, pick hops[-1] (right-most).
    3. Fallback to request.remote.
    """
    expected_secret = (
        origin_secret
        if origin_secret is not None
        else os.getenv("ORIGIN_SHARED_SECRET", "")
    )
    headers = request.headers

    if expected_secret:
        provided_secret = headers.get("X-Recluse-Origin-Secret", "")
        if safe_compare(provided_secret, expected_secret):
            worker_ip = normalize_ip(headers.get("X-Recluse-Client-IP", ""))
            if worker_ip != "invalid":
                return worker_ip, True, "origin_header"
        # Direct-to-origin bypass when ORIGIN_SHARED_SECRET is enforced:
        # Only trust the immediate Render edge hop (1 from the right), never multi-hop XFF.
        effective_hops = 1 if trusted_hops is None else max(0, min(1, trusted_hops))
    else:
        effective_hops = _parse_trusted_hops(trusted_hops)

    if effective_hops > 0:
        xff_raw = headers.get("X-Forwarded-For", "")
        if xff_raw:
            hops = [part.strip() for part in xff_raw.split(",") if part.strip()]
            if hops:
                if len(hops) >= effective_hops:
                    candidate = hops[-effective_hops]
                else:
                    # Never fall back to hops[0]; always use the right-most hop
                    candidate = hops[-1]
                norm_xff = normalize_ip(candidate)
                if norm_xff != "invalid":
                    return norm_xff, not bool(expected_secret), "xff_right"

    remote_norm = normalize_ip(getattr(request, "remote", None))
    if remote_norm != "invalid":
        return remote_norm, not bool(expected_secret), "remote"

    return "invalid", False, "none"


def get_client_ip(
    request: web.Request,
    *,
    trusted_hops: int | None = None,
    origin_secret: str | None = None,
) -> str:
    """Return the normalized client IP string (or 'invalid')."""
    ip_addr, _, _ = resolve_client_ip_details(
        request,
        trusted_hops=trusted_hops,
        origin_secret=origin_secret,
    )
    return ip_addr


class InMemoryRateLimiter:
    """
    Bounded in-memory sliding-window rate limiter using two isolated OrderedDict pools:
    - `_auth_buckets`: for validated authenticated sessions/users (`user:...` / `sid:...`)
    - `_anon_buckets`: for unauthenticated IP / shared buckets (`ip:...` / `shared:...`)
    Unauthenticated floods (e.g. rotating junk cookies or spoofed IPs) can NEVER evict
    authenticated users' rate-limit buckets. Zero MongoDB reads or writes.
    """

    DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
        "auth": (10, 60.0),          # 10 req / 60s for login/callback/logout
        "mutation": (30, 60.0),      # 30 req / 60s for POST/PUT/PATCH/DELETE
        "page": (60, 60.0),          # 60 req / 60s for authenticated GET pages
        "pre_session": (100, 60.0),  # 100 req / 60s generous IP pre-limit before session DB lookup
        "unidentified": (300, 60.0), # Generous shared bucket when client IP is unknown
    }

    def __init__(
        self,
        max_keys: int = 5000,
        limits: dict[str, tuple[int, float]] | None = None,
    ) -> None:
        self.max_keys = max(100, int(max_keys))
        self.limits = dict(self.DEFAULT_LIMITS)
        if limits:
            self.limits.update(limits)
            if "pre_session" not in limits and "page" in limits:
                page_count, page_window = self.limits["page"]
                self.limits["pre_session"] = (max(page_count * 2, 20), page_window)
        self._auth_buckets: collections.OrderedDict[str, collections.deque[float]] = (
            collections.OrderedDict()
        )
        self._anon_buckets: collections.OrderedDict[str, collections.deque[float]] = (
            collections.OrderedDict()
        )

    @staticmethod
    def _is_authenticated_identifier(identifier: str, authenticated: bool | None) -> bool:
        if authenticated is not None:
            return bool(authenticated)
        return identifier.startswith("user:") or identifier.startswith("sid:")

    def check(
        self,
        scope: str,
        identifier: str,
        *,
        now: float | None = None,
        authenticated: bool | None = None,
    ) -> tuple[bool, int]:
        """
        Record a hit for (scope, identifier).
        Returns (allowed: bool, retry_after_seconds: int).
        """
        max_reqs, window_secs = self.limits.get(scope, self.limits["page"])
        current_time = time.monotonic() if now is None else now
        bucket_key = f"{scope}:{identifier}"

        is_auth = self._is_authenticated_identifier(identifier, authenticated)
        store = self._auth_buckets if is_auth else self._anon_buckets

        dq = store.get(bucket_key)
        if dq is not None:
            store.move_to_end(bucket_key)
        else:
            if len(store) >= self.max_keys:
                store.popitem(last=False)
            dq = collections.deque()
            store[bucket_key] = dq

        cutoff = current_time - window_secs
        while dq and dq[0] <= cutoff:
            dq.popleft()

        if len(dq) >= max_reqs:
            oldest = dq[0]
            retry_after = max(1, int((oldest + window_secs) - current_time + 0.999))
            return False, retry_after

        dq.append(current_time)
        return True, 0

    def has_bucket(self, scope: str, identifier: str) -> bool:
        bucket_key = f"{scope}:{identifier}"
        return bucket_key in self._auth_buckets or bucket_key in self._anon_buckets

    def reset(self) -> None:
        self._auth_buckets.clear()
        self._anon_buckets.clear()

    @property
    def auth_bucket_count(self) -> int:
        return len(self._auth_buckets)

    @property
    def anon_bucket_count(self) -> int:
        return len(self._anon_buckets)

    def __len__(self) -> int:
        return len(self._auth_buckets) + len(self._anon_buckets)


def resolve_rate_limit_key(
    request: web.Request,
    scope: str,
    *,
    client_ip: str | None = None,
    ip_trusted: bool = True,
    session_doc: dict[str, Any] | None = None,
    user_id: int | str | None = None,
) -> tuple[str, str]:
    """
    Determine (effective_scope, identifier) for rate limiting.

    CRITICAL SECURITY RULE:
    - Never key on raw unvalidated cookie values from `request.cookies` (clients can
      rotate junk cookies to bypass rate limits and evict buckets).
    - Use `user:<discord_id>` or `sid:<session_id_hash[:16]>` ONLY when `session_doc`
      is a validated session dict (or an authenticated `user_id` is explicitly supplied).
    - For unauthenticated requests, key by trusted `client_ip`, or fall back to the
      shared `unidentified` bucket if the IP cannot be established.
    """
    resolved_ip = client_ip if client_ip is not None else get_client_ip(request)
    usable_ip = resolved_ip != "invalid" and ip_trusted

    valid_session = isinstance(session_doc, dict) and bool(session_doc)
    effective_user_id = (
        session_doc.get("discord_id")
        if valid_session and session_doc.get("discord_id") is not None
        else user_id
    )
    validated_sid_hash = ""
    if valid_session:
        raw_hash = session_doc.get("session_id_hash")
        if isinstance(raw_hash, str) and raw_hash:
            validated_sid_hash = raw_hash[:16]

    if scope == "auth":
        if usable_ip:
            return "auth", f"ip:{resolved_ip}"
        if validated_sid_hash:
            return "auth", f"sid:{validated_sid_hash}"
        if effective_user_id is not None and str(effective_user_id):
            return "auth", f"user:{effective_user_id}"
        return "unidentified", "shared:auth"

    if effective_user_id is not None and str(effective_user_id):
        return scope, f"user:{effective_user_id}"
    if validated_sid_hash:
        return scope, f"sid:{validated_sid_hash}"
    if usable_ip:
        return scope, f"ip:{resolved_ip}"
    return "unidentified", f"shared:{scope}"


def generate_csrf_token() -> str:
    """Generate a 32-byte URL-safe CSRF token."""
    return secrets.token_urlsafe(32)


def verify_csrf_token(expected_token: str | None, provided_token: str | None) -> bool:
    """Constant-time verification of a CSRF token (safe on non-ASCII/surrogates)."""
    return safe_compare(expected_token, provided_token)


async def extract_request_csrf_token(request: web.Request) -> str | None:
    """
    Extract CSRF token from X-CSRF-Token header, form field 'csrf_token',
    or JSON body 'csrf_token'.
    """
    header_token = request.headers.get("X-CSRF-Token")
    if header_token:
        return header_token.strip()

    content_type = request.content_type.lower() if request.content_type else ""
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("csrf_token"), str):
                return body["csrf_token"].strip()
        except Exception:
            return None
    elif (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        try:
            post_data = await request.post()
            val = post_data.get("csrf_token")
            if isinstance(val, str):
                return val.strip()
        except Exception:
            return None
    return None


def apply_security_headers(
    response: web.StreamResponse,
    *,
    csp: str = DEFAULT_CSP,
) -> None:
    """Attach standard security headers to an aiohttp response."""
    headers = response.headers
    headers.setdefault("Content-Security-Policy", csp)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")


def _rate_limit_response(retry_after: int) -> web.Response:
    resp = web.json_response(
        {"error": "Rate limit exceeded. Please slow down.", "retry_after": retry_after},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )
    apply_security_headers(resp)
    return resp


def _is_truthy_env(var_name: str, default: str = "0") -> bool:
    return os.getenv(var_name, default).strip().lower() in ("1", "true", "yes", "on")


def _warn_missing_origin_secret_once() -> None:
    global _MISSING_ORIGIN_SECRET_WARNED
    if not _MISSING_ORIGIN_SECRET_WARNED:
        _MISSING_ORIGIN_SECRET_WARNED = True
        logger.warning(
            "webdash: ORIGIN_SHARED_SECRET is unset; Cloudflare Worker overwrites X-Forwarded-For "
            "with CF-Connecting-IP, so right-most XFF resolution on Render hands all Worker-proxied "
            "visitors the Cloudflare egress IP and shares one auth rate-limit bucket."
        )


def _error_require_without_origin_secret_once() -> None:
    global _REQUIRE_WITHOUT_ORIGIN_SECRET_ERRORED
    if not _REQUIRE_WITHOUT_ORIGIN_SECRET_ERRORED:
        _REQUIRE_WITHOUT_ORIGIN_SECRET_ERRORED = True
        logger.error(
            "webdash: REQUIRE_ORIGIN_SHARED_SECRET is enabled while ORIGIN_SHARED_SECRET is empty; "
            "every non-fast-path request will be rejected with 403 until ORIGIN_SHARED_SECRET is set."
        )


def create_security_middleware(
    rate_limiter: InMemoryRateLimiter,
    *,
    session_loader: Callable[[web.Request], Awaitable[dict[str, Any] | None]] | None = None,
    ip_ban_cache: Any | None = None,
    csrf_exempt_paths: set[str] | None = None,
    trusted_hops: int | None = None,
    origin_secret: str | None = None,
    require_origin_secret: bool | None = None,
) -> Callable[[web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]], Awaitable[web.StreamResponse]]:
    """
    Factory for the aiohttp security middleware:
    - Logs a one-time warning if ORIGIN_SHARED_SECRET is unset
    - Logs a one-time error if REQUIRE_ORIGIN_SHARED_SECRET is enabled while ORIGIN_SHARED_SECRET is empty
    - Fast-paths GET /health and unauthenticated GET /
    - Resolves client IP (right-most XFF / ORIGIN_SHARED_SECRET)
    - Enforces REQUIRE_ORIGIN_SHARED_SECRET (blocks direct-origin requests with 403 except /health and unauth /)
    - Enforces IpBanCache against the TRUSTED client IP only (never blocking GET /health or unauthenticated GET /, failing open on DB error)
    - Cheap pre-check: skips session_loader when `recluse_sid` does not match `^[A-Za-z0-9_-]{43}$`
    - Applies an IP-keyed (or shared 'unidentified') pre-limit BEFORE calling session_loader
    - Enforces dual-pool bounded in-memory rate limits
    - Enforces CSRF on state-changing methods (POST/PUT/PATCH/DELETE)
    - Applies security headers to every response AND to raised web.HTTPException responses
    """
    exempt_paths = set(csrf_exempt_paths or set())

    initial_secret = (
        origin_secret
        if origin_secret is not None
        else os.getenv("ORIGIN_SHARED_SECRET", "")
    )
    initial_require = (
        require_origin_secret
        if require_origin_secret is not None
        else _is_truthy_env("REQUIRE_ORIGIN_SHARED_SECRET", "0")
    )
    if not initial_secret:
        _warn_missing_origin_secret_once()
        if initial_require:
            _error_require_without_origin_secret_once()

    async def _call_handler_with_headers(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            apply_security_headers(exc)
            raise
        apply_security_headers(response)
        return response

    @web.middleware
    async def _middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        effective_secret = (
            origin_secret
            if origin_secret is not None
            else os.getenv("ORIGIN_SHARED_SECRET", "")
        )
        if not effective_secret:
            _warn_missing_origin_secret_once()

        client_ip, ip_trusted, ip_source = resolve_client_ip_details(
            request,
            trusted_hops=trusted_hops,
            origin_secret=effective_secret,
        )
        request[CLIENT_IP_KEY] = client_ip
        request[CLIENT_IP_TRUSTED_KEY] = ip_trusted
        request[CLIENT_IP_SOURCE_KEY] = ip_source
        request[SESSION_DATA_KEY] = None

        path = request.path
        method = request.method.upper()

        # 1. Cheap regex pre-check on recluse_sid before any session lookup
        raw_sid = request.cookies.get("recluse_sid")
        has_plausible_cookie = is_plausible_session_token(raw_sid)

        # 2. Fast path for keep-alive probes: GET /health and GET / without a plausible session cookie
        if method in ("GET", "HEAD") and (
            path == "/health" or (path == "/" and not has_plausible_cookie)
        ):
            return await _call_handler_with_headers(request, handler)

        # 3. Optional REQUIRE_ORIGIN_SHARED_SECRET enforcement:
        #    When enabled, block any non-fast-path request that does not carry a valid
        #    X-Recluse-Origin-Secret matching ORIGIN_SHARED_SECRET.
        must_require_secret = (
            require_origin_secret
            if require_origin_secret is not None
            else _is_truthy_env("REQUIRE_ORIGIN_SHARED_SECRET", "0")
        )
        if must_require_secret:
            if not effective_secret:
                _error_require_without_origin_secret_once()
            provided_secret = request.headers.get("X-Recluse-Origin-Secret", "")
            if not effective_secret or not safe_compare(provided_secret, effective_secret):
                resp = web.json_response(
                    {"error": "Forbidden: Direct origin access is disabled."},
                    status=403,
                )
                apply_security_headers(resp)
                return resp

        # 3b. Enforce IpBanCache against the TRUSTED client IP only (never on GET /health
        #     or unauthenticated GET / which returned in step 2; fails open on error).
        if ip_ban_cache is not None and ip_trusted and client_ip != "invalid":
            try:
                is_ip_banned = await ip_ban_cache.is_banned(client_ip)
            except Exception:
                is_ip_banned = False
            if is_ip_banned:
                if path.startswith("/api/"):
                    resp = web.json_response(
                        {"error": "Forbidden: Your IP address has been restricted."},
                        status=403,
                    )
                else:
                    resp = web.Response(
                        text="403 Forbidden: Your IP address has been restricted.",
                        status=403,
                        content_type="text/plain",
                    )
                apply_security_headers(resp)
                return resp

        # 4. Determine route rate-limit scope
        if path in ("/login", "/callback", "/logout"):
            raw_scope = "auth"
        elif method not in SAFE_HTTP_METHODS:
            raw_scope = "mutation"
        else:
            raw_scope = "page"

        usable_ip = client_ip != "invalid" and ip_trusted
        session_doc: dict[str, Any] | None = None

        # 5. Pre-limit BEFORE session_loader so a flood of 43-char junk cookies cannot
        #    trigger unbounded MongoDB lookups.
        if raw_scope == "auth":
            auth_scope, auth_key = resolve_rate_limit_key(
                request,
                "auth",
                client_ip=client_ip,
                ip_trusted=ip_trusted,
                session_doc=None,
            )
            allowed, retry_after = rate_limiter.check(
                auth_scope,
                auth_key,
                authenticated=False,
            )
            if not allowed:
                return _rate_limit_response(retry_after)

            if session_loader is not None and has_plausible_cookie:
                session_doc = await session_loader(request)
                request[SESSION_DATA_KEY] = session_doc
        else:
            if session_loader is not None and has_plausible_cookie:
                pre_scope = "pre_session" if usable_ip else "unidentified"
                pre_key = f"ip:{client_ip}" if usable_ip else "shared:pre_session"
                pre_ok, pre_retry = rate_limiter.check(
                    pre_scope,
                    pre_key,
                    authenticated=False,
                )
                if not pre_ok:
                    return _rate_limit_response(pre_retry)

                session_doc = await session_loader(request)
                request[SESSION_DATA_KEY] = session_doc

            eff_scope, rl_identifier = resolve_rate_limit_key(
                request,
                raw_scope,
                client_ip=client_ip,
                ip_trusted=ip_trusted,
                session_doc=session_doc,
            )
            allowed, retry_after = rate_limiter.check(
                eff_scope,
                rl_identifier,
                authenticated=bool(session_doc),
            )
            if not allowed:
                return _rate_limit_response(retry_after)

        # 6. Enforce CSRF on unsafe methods unless path is explicitly exempt
        if method not in SAFE_HTTP_METHODS and path not in exempt_paths:
            if not session_doc:
                resp = web.json_response({"error": "Unauthorized"}, status=401)
                apply_security_headers(resp)
                return resp
            expected_csrf = session_doc.get("csrf_token")
            provided_csrf = await extract_request_csrf_token(request)
            if not verify_csrf_token(expected_csrf, provided_csrf):
                resp = web.json_response(
                    {"error": "Forbidden: Invalid or missing CSRF token"},
                    status=403,
                )
                apply_security_headers(resp)
                return resp

        return await _call_handler_with_headers(request, handler)

    return _middleware

