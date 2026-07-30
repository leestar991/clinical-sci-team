"""OSS (Alibaba Cloud Object Storage Service) backend.

Implements :class:`StorageBackend` on top of the official ``oss2`` SDK,
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

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_WORKERS = 16

_DEFAULT_READ_SEM = 12
_DEFAULT_WRITE_SEM = 8
_DEFAULT_HEAD_SEM = 16
_DEFAULT_LIST_SEM = 4


def _map_oss_error(exc: Exception) -> TosStorageError:
    """Map an ``oss2`` SDK exception to the unified hierarchy."""
    import oss2

    if isinstance(exc, oss2.exceptions.NoSuchKey):
        return TosNotFoundError(str(exc))
    if isinstance(exc, oss2.exceptions.ServerError):
        status = getattr(exc, "status", None)
        if status is not None:
            status = int(status)
            if status == 404:
                return TosNotFoundError(str(exc))
            if 400 <= status < 500:
                return TosClientError(str(exc))
        return TosServerError(str(exc))
    if isinstance(exc, oss2.exceptions.RequestError):
        return TosTimeoutError(str(exc))

    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return TosTimeoutError(str(exc))

    return TosStorageError(str(exc))


def _is_retryable(exc: Exception) -> bool:
    """True if the exception should trigger a retry."""
    import oss2

    if isinstance(exc, (TosServerError, TosTimeoutError)):
        return True
    if isinstance(exc, oss2.exceptions.ServerError):
        status = getattr(exc, "status", None)
        return status is not None and int(status) >= 500
    if isinstance(exc, oss2.exceptions.RequestError):
        return True
    return False


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, TosNotFoundError):
        return True
    import oss2

    if isinstance(exc, oss2.exceptions.NoSuchKey):
        return True
    if isinstance(exc, oss2.exceptions.ServerError):
        status = getattr(exc, "status", None)
        return status is not None and int(status) == 404
    return False


class _OssClientSingleton:
    """Thread-safe singleton for oss2.Bucket."""

    _bucket = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, endpoint: str, access_key: str, secret_key: str, bucket_name: str):
        if cls._bucket is None:
            with cls._lock:
                if cls._bucket is None:
                    import oss2

                    auth = oss2.Auth(access_key, secret_key)
                    cls._bucket = oss2.Bucket(auth, endpoint, bucket_name)
        return cls._bucket

    @classmethod
    def reset(cls):
        """Reset singleton (mainly for testing)."""
        with cls._lock:
            cls._bucket = None


class OssStorageBackend(StorageBackend):
    """OSS object-storage backend with retry, timeout, and concurrency control."""

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
        self._bucket_name = bucket
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._prefix = prefix.rstrip("/")
        self._timeout = timeout

        self._bucket = _OssClientSingleton.get(endpoint, access_key, secret_key, bucket)

        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Per-event-loop semaphores. asyncio.Semaphore is loop-affinity —
        # it binds to whichever loop first awaits on it — so a backend
        # singleton shared across the main event loop and any ad-hoc child
        # loops (e.g. the one _run_async spawns in a ThreadPoolExecutor to
        # bridge sync shims like ManifestSkillStorage.load_skills) cannot
        # reuse a single Semaphore. We keep the capacities and lazily
        # create a per-loop Semaphore on first access, indexed by the loop
        # object so GC of the loop drops its entry automatically.
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
        """Shut down the executor."""
        self._executor.shutdown(wait=False)

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
                    mapped = _map_oss_error(exc)
                    last_err = mapped
                    if is_put and attempt < _MAX_RETRIES:
                        if key is not None:
                            try:
                                await self.head(key)
                                return ""
                            except TosNotFoundError:
                                pass
                        if not _is_retryable(mapped):
                            raise mapped
                    elif not _is_retryable(mapped):
                        raise mapped

                if attempt < _MAX_RETRIES:
                    wait_s = _RETRY_BACKOFF[attempt]
                    logger.debug(
                        "Retry %d/%d after %.1fs for %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        wait_s,
                        last_err,
                    )
                    await asyncio.sleep(wait_s)

        if ok_if_not_found_after_retry and _is_not_found(last_err):
            return None
        if last_err:
            raise last_err
        raise TosStorageError("Unknown error after retries")

    # -- CRUD -----------------------------------------------------------------

    async def read(self, key: str) -> bytes:
        full_key = self._full_key(key)

        def _do():
            resp = self._bucket.get_object(full_key)
            return resp.read()

        try:
            return await self._call_with_retry(self._get_read_sem(), _do)
        except TosNotFoundError:
            raise
        except TosStorageError:
            raise
        except Exception as exc:
            mapped = _map_oss_error(exc)
            if isinstance(mapped, TosNotFoundError):
                raise TosNotFoundError(f"Key not found: {key}")
            raise mapped

    async def open_stream(self, key: str, *, chunk_size: int = 64 * 1024):
        """Async-yield *key*'s bytes in ``chunk_size`` slices via OSS streaming.

        The OSS SDK's ``get_object`` response object supports incremental
        ``read(n)``. We pull the whole response object inside the read
        semaphore, then yield chunks back to the caller — the connection
        stays open between yields, so we surrender the semaphore explicitly
        once the stream is fully consumed.
        """
        import asyncio as _asyncio

        full_key = self._full_key(key)

        async with self._get_read_sem():
            try:
                resp = await _asyncio.to_thread(self._bucket.get_object, full_key)
            except TosNotFoundError:
                raise
            except TosStorageError:
                raise
            except Exception as exc:
                mapped = _map_oss_error(exc)
                if isinstance(mapped, TosNotFoundError):
                    raise TosNotFoundError(f"Key not found: {key}") from exc
                raise mapped from exc

            try:
                while True:
                    chunk = await _asyncio.to_thread(resp.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                # OSS SDK responses are HTTP body wrappers — close to free the
                # underlying socket promptly.
                close = getattr(resp, "close", None)
                if callable(close):
                    await _asyncio.to_thread(close)

    async def write(self, key: str, data: bytes) -> str:
        full_key = self._full_key(key)

        def _do():
            resp = self._bucket.put_object(full_key, data)
            return getattr(resp, "etag", "")

        try:
            return await self._call_with_retry(
                self._get_write_sem(),
                _do,
                is_put=True,
                key=key,
            )
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_oss_error(exc)

    async def cas_write(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        """OSS conditional PUT (T-CAS-3).

        Uses native OSS headers:
        - ``x-oss-forbid-overwrite: true`` for ``if_none_match=True``
          (OSS-specific; SDK 2.13+).
        - ``If-Match: <etag>`` for ``if_match=<etag>`` (RFC 7232,
          supported by OSS Python SDK 2.18+).

        Errors flagged as 412 Precondition Failed or 409 Conflict by OSS
        map to :class:`CASMismatchError` with the offending key and the
        caller-provided expected ETag. The actual ETag is best-effort —
        OSS does not include it in the 412 body, so we head the object
        on rejection to surface a useful value.
        """
        if if_match is not None and if_none_match:
            raise ValueError("if_match and if_none_match are mutually exclusive")

        full_key = self._full_key(key)
        headers: dict[str, str] = {}
        if if_none_match:
            headers["x-oss-forbid-overwrite"] = "true"
        if if_match is not None:
            headers["If-Match"] = if_match

        def _do():
            resp = self._bucket.put_object(full_key, data, headers=headers)
            return getattr(resp, "etag", "")

        try:
            return await self._call_with_retry(
                self._get_write_sem(),
                _do,
                is_put=True,
                key=key,
            )
        except TosClientError as exc:
            # OSS surfaces conditional-PUT rejection through a client
            # error. Reach for the underlying status if accessible to
            # decide whether to translate.
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
            raise _map_oss_error(exc) from exc

    async def delete(self, key: str) -> None:
        full_key = self._full_key(key)

        def _do():
            self._bucket.delete_object(full_key)

        try:
            await self._call_with_retry(
                self._get_write_sem(),
                _do,
                ok_if_not_found_after_retry=True,
            )
        except TosNotFoundError:
            pass
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_oss_error(exc)

    async def head(self, key: str) -> HeadResult:
        full_key = self._full_key(key)

        def _do():
            return self._bucket.head_object(full_key)

        try:
            resp = await self._call_with_retry(self._get_head_sem(), _do)
            return HeadResult(
                size=int(resp.content_length or 0),
                last_modified=resp.last_modified or "",
                etag=getattr(resp, "etag", ""),
            )
        except TosNotFoundError:
            raise
        except TosStorageError:
            raise
        except Exception as exc:
            mapped = _map_oss_error(exc)
            if isinstance(mapped, TosNotFoundError):
                raise TosNotFoundError(f"Key not found: {key}")
            raise mapped

    async def list(
        self,
        prefix: str,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> ListResult:

        full_prefix = self._full_key(prefix)

        def _do():
            kwargs = {
                "prefix": full_prefix,
                "max_keys": min(max_keys, 1000),
            }
            if continuation_token:
                kwargs["continuation_token"] = continuation_token
            resp = self._bucket.list_objects_v2(**kwargs)
            keys = [obj.key for obj in resp.object_list]
            return keys, resp.is_truncated, getattr(resp, "next_continuation_token", None)

        try:
            keys, is_truncated, next_token = await self._call_with_retry(self._get_list_sem(), _do)
            stripped = []
            for k in keys:
                s = self._strip_prefix(k)
                if s != prefix and s:
                    stripped.append(s)
            return ListResult(
                keys=stripped,
                continuation_token=next_token,
                is_truncated=is_truncated,
            )
        except TosStorageError:
            raise
        except Exception as exc:
            raise _map_oss_error(exc)


def _is_precondition_failure(exc: Exception) -> bool:
    """True iff *exc* looks like an OSS conditional-PUT rejection.

    OSS surfaces:
    - HTTP 412 (Precondition Failed) for ``If-Match`` mismatch.
    - HTTP 409 (Conflict) for ``x-oss-forbid-overwrite`` rejection
      (some SDK versions use 412 here too).

    We accept both the unwrapped ``oss2`` exception and our mapped
    :class:`TosClientError` (which preserves the HTTP status via
    ``getattr(exc, 'status', None)``).
    """
    status = getattr(exc, "status", None)
    if status is None:
        # TosClientError stringifies the original exception; cheap status check.
        text = str(exc)
        return "412" in text or "PreconditionFailed" in text or "FileAlreadyExists" in text
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
