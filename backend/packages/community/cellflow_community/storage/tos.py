"""TOS (Tencent Object Storage / Volcano Engine TOS) backend.

Implements :class:`StorageBackend` on top of the official ``tos`` SDK,
with retry, timeout, and semaphore-based concurrency control.
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from weakref import WeakKeyDictionary

from deerflow.storage.base import HeadResult, ListResult, StorageBackend
from deerflow.storage.exceptions import (
    CASMismatchError,
    TosClientError,
    TosNotFoundError,
    TosServerError,
    TosStorageError,
    TosTimeoutError,
)

logger = logging.getLogger(__name__)

# Retry configuration
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]  # seconds
_DEFAULT_TIMEOUT = 30  # seconds
_DEFAULT_MAX_WORKERS = 16

# Semaphore defaults
_DEFAULT_READ_SEM = 12
_DEFAULT_WRITE_SEM = 8
_DEFAULT_HEAD_SEM = 16
_DEFAULT_LIST_SEM = 4


def _map_tos_error(exc: Exception) -> TosStorageError:
    """Map a ``tos`` SDK exception to our unified hierarchy."""
    # Try to extract HTTP status from tos exceptions
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None:
        status = int(status)
        if status == 404:
            return TosNotFoundError(str(exc))
        if 400 <= status < 500:
            return TosClientError(str(exc))
        if 500 <= status < 600:
            return TosServerError(str(exc))

    # Catch common timeout patterns
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return TosTimeoutError(str(exc))

    # Fallback
    return TosStorageError(str(exc))


def _is_retryable(exc: Exception) -> bool:
    """True if the exception should trigger a retry."""
    if isinstance(exc, (TosServerError, TosTimeoutError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None:
        return int(status) >= 500
    return False


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, TosNotFoundError):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status is not None and int(status) == 404


class _TosClientSingleton:
    """Thread-safe singleton for TosClientV2."""

    _client = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, endpoint: str, access_key: str, secret_key: str, region: str):
        if cls._client is None:
            with cls._lock:
                if cls._client is None:
                    import tos

                    cls._client = tos.TosClientV2(
                        ak=access_key,
                        sk=secret_key,
                        endpoint=endpoint,
                        region=region,
                        enable_crc=False,  # Disable CRC for performance
                    )
        return cls._client

    @classmethod
    def reset(cls):
        """Reset singleton (mainly for testing)."""
        with cls._lock:
            cls._client = None


class TosStorageBackend(StorageBackend):
    """TOS object-storage backend with retry, timeout, and concurrency control."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str,
        prefix: str = "cellflow",
        timeout: float = _DEFAULT_TIMEOUT,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        read_sem: int = _DEFAULT_READ_SEM,
        write_sem: int = _DEFAULT_WRITE_SEM,
        head_sem: int = _DEFAULT_HEAD_SEM,
        list_sem: int = _DEFAULT_LIST_SEM,
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._prefix = prefix.rstrip("/")
        self._timeout = timeout

        self._client = _TosClientSingleton.get(endpoint, access_key, secret_key, region)

        # Dedicated thread pool — tos SDK is synchronous
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Per-event-loop semaphores — see OssStorageBackend for the same
        # rationale: asyncio.Semaphore is loop-affinity and this backend
        # singleton is shared across the main event loop and any ad-hoc
        # child loops (e.g. _run_async's ThreadPoolExecutor loop bridging
        # sync shims). Keep capacities and lazily create per-loop
        # Semaphores keyed on the loop object so GC drops entries.
        self._read_sem_capacity = read_sem
        self._write_sem_capacity = write_sem
        self._head_sem_capacity = head_sem
        self._list_sem_capacity = list_sem
        self._sem_lock = threading.Lock()
        self._read_sems: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
        self._write_sems: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
        self._head_sems: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
        self._list_sems: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()

    def _get_sem_for_loop(
        self,
        pool: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]",
        capacity: int,
    ) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._sem_lock:
            sem = pool.get(loop)
            if sem is None:
                sem = asyncio.Semaphore(capacity)
                pool[loop] = sem
            return sem

    def _get_read_sem(self) -> asyncio.Semaphore:
        return self._get_sem_for_loop(self._read_sems, self._read_sem_capacity)

    def _get_write_sem(self) -> asyncio.Semaphore:
        return self._get_sem_for_loop(self._write_sems, self._write_sem_capacity)

    def _get_head_sem(self) -> asyncio.Semaphore:
        return self._get_sem_for_loop(self._head_sems, self._head_sem_capacity)

    def _get_list_sem(self) -> asyncio.Semaphore:
        return self._get_sem_for_loop(self._list_sems, self._list_sem_capacity)

    def _full_key(self, key: str) -> str:
        """Prepend the configured prefix."""
        prefix = self._prefix
        if prefix:
            return f"{prefix}/{key}"
        return key

    def _strip_prefix(self, full_key: str) -> str:
        """Strip the configured prefix from a key returned by list."""
        prefix = self._prefix
        if prefix and full_key.startswith(prefix + "/"):
            return full_key[len(prefix) + 1 :]
        return full_key

    async def shutdown(self) -> None:
        """Shut down the executor. Call when backend is no longer needed."""
        self._executor.shutdown(wait=False)

    # -- retry / call wrapper --------------------------------------------------

    async def _call_with_retry(
        self,
        sem: asyncio.Semaphore,
        fn,
        *args,
        is_put: bool = False,
        key: str | None = None,
        ok_if_not_found_after_retry: bool = False,
    ):
        """Wrap a synchronous SDK call with semaphore, thread offload, retry, and timeout."""
        last_err: Exception | None = None

        async with sem:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(fn, *args),
                        timeout=self._timeout,
                    )
                except TimeoutError:
                    last_err = TosTimeoutError("Operation timed out")
                except Exception as exc:
                    mapped = _map_tos_error(exc)
                    last_err = mapped
                    # PUT is idempotent — retry even on ambiguous errors
                    if is_put and attempt < _MAX_RETRIES:
                        # Check if the object was already written
                        if key is not None:
                            try:
                                await self.head(key)
                                # Object exists → the PUT likely succeeded
                                return ""
                            except TosNotFoundError:
                                pass
                        if not _is_retryable(mapped):
                            raise mapped
                    elif not _is_retryable(mapped):
                        raise mapped

                if attempt < _MAX_RETRIES:
                    wait_s = _RETRY_BACKOFF[attempt]
                    logger.debug("Retry %d/%d after %.1fs for %s", attempt + 1, _MAX_RETRIES, wait_s, last_err)
                    await asyncio.sleep(wait_s)

        # After retries exhausted
        if ok_if_not_found_after_retry and _is_not_found(last_err):
            return None
        if last_err:
            raise last_err
        raise TosStorageError("Unknown error after retries")

    # -- CRUD -----------------------------------------------------------------

    async def read(self, key: str) -> bytes:
        import tos

        full_key = self._full_key(key)

        def _do():
            resp = self._client.get_object(bucket=self._bucket, key=full_key)
            return resp.read()

        try:
            return await self._call_with_retry(self._get_read_sem(), _do)
        except tos.exceptions.TosServerError as exc:
            if exc.status_code == 404:
                raise TosNotFoundError(f"Key not found: {key}")
            raise _map_tos_error(exc)
        except TosNotFoundError:
            raise
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_tos_error(exc)

    async def open_stream(self, key: str, *, chunk_size: int = 64 * 1024):
        """Async-yield *key*'s bytes in ``chunk_size`` slices via TOS streaming.

        Mirrors :meth:`OssStorageBackend.open_stream`: pull the response inside
        the read semaphore, yield chunks until the body is drained, close the
        underlying response so the socket returns to the pool promptly.
        """
        import asyncio as _asyncio

        import tos

        full_key = self._full_key(key)

        async with self._get_read_sem():
            try:
                resp = await _asyncio.to_thread(self._client.get_object, bucket=self._bucket, key=full_key)
            except tos.exceptions.TosServerError as exc:
                if exc.status_code == 404:
                    raise TosNotFoundError(f"Key not found: {key}") from exc
                raise _map_tos_error(exc) from exc
            except TosNotFoundError:
                raise
            except TosStorageError:
                raise
            except Exception as exc:
                raise _map_tos_error(exc) from exc

            try:
                while True:
                    chunk = await _asyncio.to_thread(resp.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    await _asyncio.to_thread(close)

    async def write(self, key: str, data: bytes) -> str:

        full_key = self._full_key(key)

        def _do():
            resp = self._client.put_object(bucket=self._bucket, key=full_key, content=data)
            return getattr(resp, "etag", "")

        try:
            return await self._call_with_retry(self._get_write_sem(), _do, is_put=True, key=key)
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_tos_error(exc)

    async def cas_write(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        """TOS conditional PUT (T-CAS-4).

        Uses RFC 7232 ``If-Match`` / ``If-None-Match`` headers, which the
        TOS Python SDK exposes as keyword arguments on ``put_object``.
        Conditional rejection arrives as HTTP 412 from the server and
        is mapped to :class:`CASMismatchError` with a best-effort actual
        ETag (recovered via a follow-up ``head``).
        """
        if if_match is not None and if_none_match:
            raise ValueError("if_match and if_none_match are mutually exclusive")

        full_key = self._full_key(key)

        def _do():
            put_kwargs: dict = {"bucket": self._bucket, "key": full_key, "content": data}
            if if_match is not None:
                put_kwargs["if_match"] = if_match
            if if_none_match:
                # RFC 7232 sentinel for "must not exist".
                put_kwargs["if_none_match"] = "*"
            resp = self._client.put_object(**put_kwargs)
            return getattr(resp, "etag", "")

        try:
            return await self._call_with_retry(self._get_write_sem(), _do, is_put=True, key=key)
        except TosClientError as exc:
            if _is_precondition_failure(exc):
                actual = await _safe_head_etag(self, key)
                raise CASMismatchError(key=key, expected_etag=if_match, actual_etag=actual) from exc
            raise
        except TosStorageError:
            raise
        except Exception as exc:
            if _is_precondition_failure(exc):
                actual = await _safe_head_etag(self, key)
                raise CASMismatchError(key=key, expected_etag=if_match, actual_etag=actual) from exc
            raise _map_tos_error(exc) from exc

    async def delete(self, key: str) -> None:
        full_key = self._full_key(key)

        def _do():
            self._client.delete_object(bucket=self._bucket, key=full_key)

        try:
            await self._call_with_retry(self._get_write_sem(), _do, ok_if_not_found_after_retry=True)
        except TosNotFoundError:
            pass  # idempotent
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_tos_error(exc)

    async def head(self, key: str) -> HeadResult:
        import tos

        full_key = self._full_key(key)

        def _do():
            return self._client.head_object(bucket=self._bucket, key=full_key)

        try:
            resp = await self._call_with_retry(self._get_head_sem(), _do)
            return HeadResult(
                size=int(resp.content_length or 0),
                last_modified=resp.last_modified or "",
                etag=getattr(resp, "etag", ""),
            )
        except tos.exceptions.TosServerError as exc:
            if exc.status_code == 404:
                raise TosNotFoundError(f"Key not found: {key}")
            raise _map_tos_error(exc)
        except TosNotFoundError:
            raise
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_tos_error(exc)

    async def list(
        self,
        prefix: str,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> ListResult:
        full_prefix = self._full_key(prefix)

        def _do():
            kwargs = {
                "bucket": self._bucket,
                "prefix": full_prefix,
                "max_keys": min(max_keys, 1000),
            }
            if continuation_token:
                kwargs["continuation_token"] = continuation_token
            return self._client.list_objects(**kwargs)

        try:
            resp = await self._call_with_retry(self._get_list_sem(), _do)
            keys = []
            for obj in resp.contents or []:
                stripped = self._strip_prefix(obj.key)
                if stripped != prefix and stripped:
                    keys.append(stripped)
            return ListResult(
                keys=keys,
                continuation_token=resp.continuation_token if resp.is_truncated else None,
                is_truncated=resp.is_truncated,
            )
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_tos_error(exc)


def _is_precondition_failure(exc: Exception) -> bool:
    """True iff *exc* looks like a TOS conditional-PUT rejection (HTTP 412)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        text = str(exc)
        return "412" in text or "PreconditionFailed" in text
    try:
        return int(status) in (409, 412)
    except (TypeError, ValueError):
        return False


async def _safe_head_etag(backend, key: str) -> str | None:
    try:
        head = await backend.head(key)
    except TosStorageError:
        return None
    return head.etag or None
