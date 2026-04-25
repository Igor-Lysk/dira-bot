"""Lazy lifecycle management for the flaresolverr docker container.

Why lazy: FlareSolverr keeps a headless Chrome alive (~280 MB RAM)
for fast warm responses. We only fall through to it when all paid
proxy providers fail — typically zero or one call per hour. Letting
it sit idle 24/7 wastes ~280 MB on a 3.7 GB VPS.

Lifecycle:
  - Container is NOT auto-started on `docker compose up` (uses profiles)
  - Each call: ensure_started() → request → stop_in_background()
  - Cold start ~5-8 sec (acceptable trade-off given hourly cadence)

Requires the host docker socket mounted into dira-bot (see docker-compose.yml).
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

CONTAINER_NAME = "flaresolverr"
HEALTH_URL = "http://flaresolverr:8191/v1"
COLD_START_TIMEOUT_SEC = 30

_lock = asyncio.Lock()


def _docker_client():
    import docker
    return docker.from_env()


async def ensure_started() -> bool:
    """Start the FlareSolverr container if needed; wait until it answers.

    Returns True when the container is up and the API endpoint is healthy,
    False on any error (caller should treat as 'provider unavailable').
    """
    async with _lock:
        try:
            cli = _docker_client()
        except Exception as e:
            log.warning("flaresolverr: docker SDK unavailable (%s)", e)
            return False

        try:
            container = cli.containers.get(CONTAINER_NAME)
        except Exception as e:
            log.warning("flaresolverr container not found: %s", e)
            return False

        container.reload()

        if container.status == "running":
            return True  # already warm — proceed

        log.info("flaresolverr cold-starting (status was %s)", container.status)
        t0 = time.time()
        try:
            container.start()
        except Exception as e:
            log.warning("flaresolverr start failed: %s", e)
            return False

        # Poll the /v1 endpoint until it responds. FlareSolverr returns
        # 405 Method Not Allowed for GET /v1, which proves it's alive.
        deadline = time.time() + COLD_START_TIMEOUT_SEC
        async with httpx.AsyncClient() as http:
            while time.time() < deadline:
                try:
                    r = await http.get(HEALTH_URL, timeout=2)
                    if r.status_code in (200, 405):
                        log.info("flaresolverr ready (cold start took %.1fs)", time.time() - t0)
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        log.warning("flaresolverr did not become healthy in %ds", COLD_START_TIMEOUT_SEC)
        return False


def stop_in_background() -> None:
    """Schedule a container stop without blocking the caller.

    `container.stop()` is synchronous and waits up to `timeout=` seconds
    for the process to exit. We don't care when that completes — by the
    time the next FlareSolverr call comes (typically 1 hour later) the
    container is long since stopped. So we fire-and-forget via a thread.
    """
    asyncio.create_task(_stop_async())


async def _stop_async() -> None:
    def _stop_sync():
        try:
            cli = _docker_client()
            container = cli.containers.get(CONTAINER_NAME)
            container.reload()
            if container.status == "running":
                container.stop(timeout=15)
                log.info("flaresolverr stopped")
        except Exception as e:
            log.warning("flaresolverr stop failed: %s", e)

    await asyncio.to_thread(_stop_sync)
