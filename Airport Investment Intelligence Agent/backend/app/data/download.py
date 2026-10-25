"""Safe HTTP access to the public data sources.

Every request goes to an HTTPS URL on an allowlisted host. Redirects are followed manually and
each hop is checked against the allowlist again. Responses are size-limited, downloads are
streamed to a temporary file and moved into place only when complete, and transient failures
(connection and DNS errors, timeouts, HTTP 429/5xx) are retried in this single layer with
exponential backoff, jitter and `Retry-After`, inside an optional overall deadline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode, urljoin

import httpx

from app.config import Settings

logger = logging.getLogger("app.data.download")

ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "taf.faa.gov",
        "www.transtats.bts.gov",
        "transtats.bts.gov",
        "data.bts.gov",
        "data.transportation.gov",
    }
)
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
NOT_FOUND_STATUS: frozenset[int] = frozenset({404, 410})
MAX_REDIRECTS = 5
MAX_TEXT_BYTES = 32 * 1024 * 1024  # HTML pages and JSON API responses
MAX_RETRY_AFTER_SECONDS = 300.0
USER_AGENT = "AirportInvestmentIntelligenceAgent/0.1 (public data refresh)"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ZIP_MAGIC = b"PK\x03\x04"

T = TypeVar("T")


class DownloadError(Exception):
    """A source could not be reached or returned an unusable response."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class DisallowedUrlError(DownloadError):
    """The URL (or a redirect target) is not an HTTPS URL on an allowlisted host."""


class ResponseTooLargeError(DownloadError):
    """The response exceeds the configured size limit."""


class RemoteNotFoundError(DownloadError):
    """The resource does not exist (HTTP 404/410 or an error page instead of the file)."""


class DeadlineExceededError(DownloadError):
    """The overall time budget was used up before the request could complete."""


class _Transient(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RemoteMeta:
    """Cheap remote metadata from a HEAD request, used as a change fingerprint."""

    url: str
    etag: str | None
    last_modified: str | None
    content_length: int | None
    content_type: str | None

    def fingerprint(self) -> str | None:
        if self.etag is None and self.last_modified is None:
            return None
        return f"etag={self.etag or ''};last-modified={self.last_modified or ''};length={self.content_length}"


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    size_bytes: int
    url: str  # final URL after redirects
    meta: RemoteMeta
    retrieved_at: datetime


def validate_url(url: str | httpx.URL, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> httpx.URL:
    """Return the parsed URL when it is HTTPS on an allowlisted host; raise otherwise."""
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise DisallowedUrlError("The source URL is not valid.") from exc
    host = (parsed.host or "").lower().rstrip(".")
    if parsed.scheme != "https":
        raise DisallowedUrlError(f"Only HTTPS source URLs are allowed (got {parsed.scheme or 'none'}).")
    if host not in allowed_hosts:
        raise DisallowedUrlError(f"Host {host or '(none)'} is not an allowed data source.")
    if parsed.userinfo or parsed.port not in (None, 443):
        raise DisallowedUrlError("Source URLs must not carry credentials or a non-standard port.")
    return parsed


def safe_child(base: Path, *parts: str) -> Path:
    """`base / parts...` where every part is a plain token and the result stays inside `base`."""
    for part in parts:
        if not _TOKEN.fullmatch(part):
            raise ValueError(f"Unsafe path component: {part!r}")
    target = base.joinpath(*parts)
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError("Path escapes the data directory.")
    return target


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta seconds or HTTP date), or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _meta(response: httpx.Response) -> RemoteMeta:
    length = response.headers.get("content-length")
    return RemoteMeta(
        url=str(response.url),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        content_length=int(length) if length and length.isdigit() else None,
        content_type=response.headers.get("content-type"),
    )


def _is_html(response: httpx.Response) -> bool:
    return (response.headers.get("content-type") or "").lower().startswith("text/html")


class SafeHttpClient:
    """One httpx client (one cookie jar) with allowlist, redirect, size, retry and deadline rules."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_retries: int,
        max_bytes: int,
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        deadline: float | None = None,
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.max_bytes = max_bytes
        self.allowed_hosts = allowed_hosts
        self.deadline = deadline  # time.monotonic() value
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._client = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            timeout=httpx.Timeout(timeout_seconds, connect=min(30.0, timeout_seconds)),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> SafeHttpClient:
        return cls(
            timeout_seconds=settings.download_timeout_seconds,
            max_retries=settings.download_max_retries,
            max_bytes=settings.download_max_bytes,
            **kwargs,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- public operations ---------------------------------------------------------------------

    def head(self, url: str) -> RemoteMeta | None:
        """Metadata of a file, or None when it is not published (404/410 or an HTML error page)."""

        def attempt() -> RemoteMeta | None:
            with self._open("HEAD", url) as response:
                if response.status_code in NOT_FOUND_STATUS:
                    return None
                self._raise_for_status(response)
                if _is_html(response):
                    return None  # e.g. taf.faa.gov redirects a missing file to /Error.html
                return _meta(response)

        return self._with_retries(attempt, url)

    def get_text(self, url: str, *, params: Mapping[str, str] | None = None) -> tuple[str, str]:
        """Body text and final URL of a small response (HTML form pages), with retries."""
        return self._with_retries(lambda: self.get_text_once(url, params=params), url)

    def get_text_once(self, url: str, *, params: Mapping[str, str] | None = None) -> tuple[str, str]:
        """One attempt of `get_text`; use inside a `download(prepare=...)` step that retries as a whole."""
        with self._open("GET", url, params=params) as response:
            if response.status_code in NOT_FOUND_STATUS:
                raise RemoteNotFoundError(f"{_host(url)} returned HTTP {response.status_code}.")
            self._raise_for_status(response)
            body = self._read_limited(response, MAX_TEXT_BYTES)
            return body.decode(response.encoding or "utf-8", errors="replace"), str(response.url)

    def get_json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any:
        def attempt() -> Any:
            with self._open("GET", url, params=params) as response:
                if response.status_code in NOT_FOUND_STATUS:
                    raise RemoteNotFoundError(f"{_host(url)} returned HTTP {response.status_code}.")
                self._raise_for_status(response)
                body = self._read_limited(response, MAX_TEXT_BYTES)
            try:
                return json.loads(body)
            except ValueError as exc:
                raise DownloadError(f"{_host(url)} returned invalid JSON.") from exc

        return self._with_retries(attempt, url)

    def download(
        self,
        url: str,
        *,
        dest_dir: Path,
        tmp_dir: Path,
        stem: str,
        suffix: str = ".zip",
        method: str = "GET",
        data: Sequence[tuple[str, str]] | None = None,
        read_timeout: float | None = None,
        expect_zip: bool = True,
        prepare: Callable[[], tuple[str, Sequence[tuple[str, str]]]] | None = None,
    ) -> DownloadResult:
        """Stream a file to `tmp_dir`, then move it to `dest_dir/{stem}-{sha12}{suffix}`.

        `prepare`, when given, runs at the start of every attempt and returns the URL and form
        body to send (used for ASP.NET forms whose hidden state must be fetched fresh each time).
        """
        if not _TOKEN.fullmatch(stem) or suffix not in (".zip", ".json", ".csv"):
            raise ValueError("Unsafe download file name.")
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        def attempt() -> DownloadResult:
            target, body = prepare() if prepare is not None else (url, data)
            tmp_path = tmp_dir / f"{stem}.{os.getpid()}.{random.getrandbits(32):08x}.part"
            digest = hashlib.sha256()
            size = 0
            try:
                with self._open(method, target, data=body, read_timeout=read_timeout) as response:
                    if response.status_code in NOT_FOUND_STATUS:
                        raise RemoteNotFoundError(f"{_host(url)} returned HTTP {response.status_code}.")
                    self._raise_for_status(response)
                    if _is_html(response) and expect_zip:
                        raise RemoteNotFoundError(f"{_host(url)} returned a web page instead of a file.")
                    meta = _meta(response)
                    if meta.content_length is not None and meta.content_length > self.max_bytes:
                        raise ResponseTooLargeError(_too_large(url, self.max_bytes))
                    with tmp_path.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise ResponseTooLargeError(_too_large(url, self.max_bytes))
                            digest.update(chunk)
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                if expect_zip:
                    with tmp_path.open("rb") as handle:
                        if handle.read(4) != _ZIP_MAGIC:
                            raise DownloadError(f"{_host(url)} did not return a ZIP file.")
                sha = digest.hexdigest()
                final = dest_dir / f"{stem}-{sha[:12]}{suffix}"
                os.replace(tmp_path, final)
                return DownloadResult(
                    path=final,
                    sha256=sha,
                    size_bytes=size,
                    url=meta.url,
                    meta=meta,
                    retrieved_at=datetime.now(UTC),
                )
            finally:
                with suppress(FileNotFoundError):
                    tmp_path.unlink()

        return self._with_retries(attempt, url)

    # -- internals -----------------------------------------------------------------------------

    @contextmanager
    def _open(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Sequence[tuple[str, str]] | None = None,
        read_timeout: float | None = None,
    ) -> Iterator[httpx.Response]:
        """Send a request and follow redirects manually, validating every hop."""
        validate_url(url, self.allowed_hosts)
        timeout = httpx.Timeout(read_timeout or self.timeout_seconds, connect=min(30.0, self.timeout_seconds))
        request = self._client.build_request(
            method,
            url,
            params=params,
            content=_form_body(data) if data is not None else None,
            headers={"Content-Type": "application/x-www-form-urlencoded"} if data is not None else None,
            timeout=timeout,
        )
        response: httpx.Response | None = None
        try:
            for _ in range(MAX_REDIRECTS + 1):
                self._check_deadline()
                try:
                    response = self._client.send(request, stream=True)
                except httpx.TransportError as exc:  # DNS, connect, read timeout, protocol
                    raise _Transient(f"{type(exc).__name__} contacting {_host(str(request.url))}") from exc
                if not response.has_redirect_location or response.next_request is None:
                    yield response
                    return
                next_request = response.next_request
                response.close()
                response = None
                validate_url(next_request.url, self.allowed_hosts)
                request = next_request
            raise DownloadError(f"Too many redirects from {_host(url)}.")
        finally:
            if response is not None:
                response.close()

    def _with_retries(self, attempt: Callable[[], T], url: str) -> T:
        retries = 0
        while True:
            self._check_deadline()
            try:
                return attempt()
            except (_Transient, httpx.TransportError) as exc:
                if retries >= self.max_retries:
                    raise DownloadError(
                        f"{_host(url)} could not be reached after {retries + 1} attempts ({exc}).",
                        retryable=True,
                    ) from exc
                delay = self._backoff(retries)
                retry_after = exc.retry_after if isinstance(exc, _Transient) else None
                if retry_after is not None:
                    if retry_after > MAX_RETRY_AFTER_SECONDS:
                        raise DownloadError(
                            f"{_host(url)} asked to retry after {retry_after:.0f} s, which is too long.",
                            retryable=True,
                        ) from exc
                    delay = max(delay, retry_after)
                if self.deadline is not None and time.monotonic() + delay >= self.deadline:
                    raise DeadlineExceededError("The refresh time budget ran out while retrying.") from exc
                logger.warning(
                    "download_retry",
                    extra={
                        "host": _host(url),
                        "attempt": retries + 1,
                        "delay_s": round(delay, 1),
                        "reason": str(exc),
                    },
                )
                self._sleep(delay)
                retries += 1

    def _backoff(self, retries: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * (2**retries))
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    def _check_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise DeadlineExceededError("The refresh time budget ran out.")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status in RETRYABLE_STATUS:
            raise _Transient(
                f"HTTP {status} from {_host(str(response.url))}",
                retry_after=parse_retry_after(response.headers.get("retry-after")),
            )
        if status != 200:
            raise DownloadError(f"{_host(str(response.url))} returned HTTP {status}.")

    @staticmethod
    def _read_limited(response: httpx.Response, limit: int) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ResponseTooLargeError(_too_large(str(response.url), limit))
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ResponseTooLargeError(_too_large(str(response.url), limit))
                chunks.append(chunk)
        except httpx.TransportError as exc:
            raise _Transient(f"{type(exc).__name__} reading from {_host(str(response.url))}") from exc
        return b"".join(chunks)


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or "the source"
    except (httpx.InvalidURL, TypeError, ValueError):
        return "the source"


def _too_large(url: str, limit: int) -> str:
    return f"The response from {_host(url)} exceeds the {limit // (1024 * 1024)} MB limit."


def _form_body(data: Sequence[tuple[str, str]]) -> bytes:
    return urlencode(list(data)).encode("ascii")


# ---------------------------------------------------------------------------------------------
# ASP.NET form helper (BTS TranStats "Download" pages)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AspNetForm:
    action_url: str
    hidden_fields: dict[str, str]


class _FormParser(HTMLParser):
    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._form_id = form_id
        self._in_form = False
        self.action: str | None = None
        self.hidden: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form" and values.get("id") == self._form_id:
            self._in_form = True
            self.action = values.get("action")
        elif tag == "input" and self._in_form and values.get("type", "").lower() == "hidden":
            name = values.get("name")
            if name:
                self.hidden[name] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


REQUIRED_ASPNET_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


def parse_aspnet_form(html: str, page_url: str, form_id: str = "form1") -> AspNetForm:
    """Hidden state fields and the resolved action URL of an ASP.NET WebForms page."""
    parser = _FormParser(form_id)
    parser.feed(html)
    missing = [name for name in REQUIRED_ASPNET_FIELDS if name not in parser.hidden]
    if parser.action is None or missing:
        raise DownloadError("The TranStats download page did not have the expected form fields.")
    return AspNetForm(action_url=urljoin(page_url, parser.action), hidden_fields=parser.hidden)


def transtats_download_payload(
    form: AspNetForm, *, year: int, fields: Sequence[str], geography: str = "All", period: str = "All"
) -> list[tuple[str, str]]:
    """POST body for a TranStats zipped CSV download with exactly the requested fields checked."""
    payload = list(form.hidden_fields.items())
    payload += [
        ("cboGeography", geography),
        ("cboYear", str(year)),
        ("cboPeriod", period),
        ("chkDownloadZip", "on"),
        ("btnDownload", "Download"),
    ]
    payload += [(field, "on") for field in fields]
    return payload
