"""Safe HTTP client: host allowlist, manual redirects, size limits, retries and the TranStats form."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.data.download import (
    DeadlineExceededError,
    DisallowedUrlError,
    DownloadError,
    RemoteNotFoundError,
    ResponseTooLargeError,
    SafeHttpClient,
    parse_aspnet_form,
    parse_retry_after,
    safe_child,
    transtats_download_payload,
    validate_url,
)
from app.data.providers import t100

FIXTURES = Path(__file__).parent / "fixtures" / "data"
TAF_URL = "https://taf.faa.gov/Downloads/APO100_TAF_Final_2025.zip"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 2,
    max_bytes: int = 1024 * 1024,
    deadline: float | None = None,
) -> tuple[SafeHttpClient, list[float]]:
    delays: list[float] = []
    client = SafeHttpClient(
        timeout_seconds=5,
        max_retries=max_retries,
        max_bytes=max_bytes,
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
        deadline=deadline,
    )
    return client, delays


@pytest.mark.parametrize(
    "url",
    [
        "http://taf.faa.gov/Downloads/x.zip",  # not HTTPS
        "https://example.com/data.zip",  # host not allowed
        "https://taf.faa.gov.example.com/x.zip",  # suffix trick
        "https://user:pw@taf.faa.gov/x.zip",  # credentials
        "https://taf.faa.gov:8443/x.zip",  # non-standard port
        "file:///etc/passwd",
    ],
)
def test_validate_url_rejects_urls_outside_the_allowlist(url: str) -> None:
    with pytest.raises(DisallowedUrlError):
        validate_url(url)


def test_validate_url_accepts_allowed_hosts() -> None:
    assert validate_url("https://data.bts.gov/resource/r495-tyji.json").host == "data.bts.gov"
    assert validate_url("https://www.transtats.bts.gov/DL_SelectFields.aspx").host == "www.transtats.bts.gov"


def test_safe_child_rejects_path_tricks(tmp_path: Path) -> None:
    assert safe_child(tmp_path, "faa_taf") == tmp_path / "faa_taf"
    for bad in ("..", "../x", "a/b", "a\\b", "", ".hidden"):
        with pytest.raises(ValueError):
            safe_child(tmp_path, bad)


def test_redirect_to_disallowed_host_is_refused_before_any_request(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example.com/payload.zip"})

    client, _ = make_client(handler)
    with pytest.raises(DisallowedUrlError):
        client.download(TAF_URL, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp", stem="2025")
    assert seen == [TAF_URL]
    assert not any((tmp_path / "out").iterdir())


def test_redirect_between_allowed_hosts_is_followed(tmp_path: Path) -> None:
    payload = zip_bytes({"a.csv": b"x"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "transtats.bts.gov":
            return httpx.Response(301, headers={"Location": "https://www.transtats.bts.gov/PREZIP/f.zip"})
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-zip-compressed"})

    client, _ = make_client(handler)
    result = client.download(
        "https://transtats.bts.gov/PREZIP/f.zip",
        dest_dir=tmp_path / "out",
        tmp_dir=tmp_path / "tmp",
        stem="2025-01",
    )
    assert result.url == "https://www.transtats.bts.gov/PREZIP/f.zip"
    assert result.path.read_bytes() == payload
    assert result.path.name == f"2025-01-{result.sha256[:12]}.zip"
    assert list((tmp_path / "tmp").iterdir()) == []  # temporary file moved into place


def test_head_treats_missing_files_and_error_pages_as_unpublished() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("2026.zip"):  # taf.faa.gov sends missing files to an error page
            return httpx.Response(302, headers={"Location": "/Error.html?aspxerrorpath=/Downloads/x.zip"})
        if request.url.path == "/Error.html":
            return httpx.Response(200, headers={"Content-Type": "text/html"})
        if request.url.path.endswith("2027.zip"):
            return httpx.Response(404, headers={"Content-Type": "text/html"})
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/x-zip-compressed",
                "ETag": '"abc"',
                "Last-Modified": "Fri, 20 Mar 2026 13:57:05 GMT",
                "Content-Length": "15670789",
            },
        )

    client, _ = make_client(handler)
    assert client.head("https://taf.faa.gov/Downloads/APO100_TAF_Final_2026.zip") is None
    assert client.head("https://taf.faa.gov/Downloads/APO100_TAF_Final_2027.zip") is None
    meta = client.head(TAF_URL)
    assert meta is not None and meta.content_length == 15670789
    assert meta.fingerprint() == 'etag="abc";last-modified=Fri, 20 Mar 2026 13:57:05 GMT;length=15670789'


def test_download_size_limit_from_content_length_and_from_stream(tmp_path: Path) -> None:
    big = b"PK\x03\x04" + b"0" * 5000

    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"Content-Length": str(len(big))})

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(big))  # no Content-Length

    for handler in (declared, streamed):
        client, _ = make_client(handler, max_bytes=1000)
        with pytest.raises(ResponseTooLargeError):
            client.download(TAF_URL, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp", stem="2025")
        assert list((tmp_path / "tmp").iterdir()) == []


def test_download_rejects_web_page_or_non_zip_content(tmp_path: Path) -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>", headers={"Content-Type": "text/html"})

    def not_zip(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"plain bytes", headers={"Content-Type": "application/octet-stream"}
        )

    client, _ = make_client(html)
    with pytest.raises(RemoteNotFoundError):
        client.download(TAF_URL, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp", stem="2025")
    client, _ = make_client(not_zip)
    with pytest.raises(DownloadError, match="did not return a ZIP"):
        client.download(TAF_URL, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp", stem="2025")


def test_transient_errors_are_retried_with_backoff_then_succeed(tmp_path: Path) -> None:
    calls = {"count": 0}
    payload = zip_bytes({"a.csv": b"x"})

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("Could not resolve host", request=request)
        if calls["count"] == 2:
            return httpx.Response(503, headers={"Retry-After": "7"})
        return httpx.Response(200, content=payload)

    client, delays = make_client(handler, max_retries=3)
    result = client.download(TAF_URL, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp", stem="2025")
    assert result.size_bytes == len(payload)
    assert calls["count"] == 3
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 2.0  # exponential backoff with jitter: base 2 s, between half and full
    assert delays[1] >= 7.0  # Retry-After honored


def test_retries_stop_at_the_limit_and_permanent_errors_are_not_retried() -> None:
    calls = {"count": 0}

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503)

    client, delays = make_client(unavailable, max_retries=2)
    with pytest.raises(DownloadError) as error:
        client.get_json("https://data.bts.gov/resource/r495-tyji.json")
    assert error.value.retryable
    assert calls["count"] == 3 and len(delays) == 2

    calls["count"] = 0

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(403)

    client, delays = make_client(forbidden, max_retries=2)
    with pytest.raises(DownloadError):
        client.get_json("https://data.bts.gov/resource/r495-tyji.json")
    assert calls["count"] == 1 and delays == []


def test_retry_after_too_long_and_deadline_stop_retrying() -> None:
    def busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    client, _ = make_client(busy, max_retries=3)
    with pytest.raises(DownloadError, match="too long"):
        client.get_json("https://data.bts.gov/resource/r495-tyji.json")

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = make_client(failing, max_retries=5, deadline=0.0)  # monotonic deadline already passed
    with pytest.raises(DeadlineExceededError):
        client.get_json("https://data.bts.gov/resource/r495-tyji.json")


def test_parse_retry_after_accepts_seconds_and_dates() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past


def test_aspnet_form_parsing_and_transtats_payload() -> None:
    html = (FIXTURES / "transtats_form.html").read_text(encoding="utf-8")
    form = parse_aspnet_form(html, t100.FORM_URL)
    assert form.action_url == t100.FORM_URL  # relative action resolved, &amp; unescaped
    assert form.hidden_fields["__VIEWSTATE"] == "dGVzdA+/viewstate=="
    assert "affiliate" not in form.hidden_fields  # fields of other forms are ignored
    payload = transtats_download_payload(form, year=2025, fields=["YEAR", "MONTH"])
    assert ("cboYear", "2025") in payload and ("cboGeography", "All") in payload
    assert ("cboPeriod", "All") in payload and ("chkDownloadZip", "on") in payload
    assert ("YEAR", "on") in payload and ("MONTH", "on") in payload
    with pytest.raises(DownloadError):
        parse_aspnet_form("<html><form id='form1' action='x'></form></html>", t100.FORM_URL)


def test_t100_download_posts_the_form_with_session_cookie_and_fields(tmp_path: Path) -> None:
    html = (FIXTURES / "transtats_form.html").read_text(encoding="utf-8")
    payload = zip_bytes({t100.CSV_MEMBER: b"YEAR\n2025\n"})
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text=html, headers={"Content-Type": "text/html", "Set-Cookie": "f5_session=abc; Path=/"}
            )
        posts.append(request)
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-zip-compressed"})

    client, _ = make_client(handler)
    result = t100.download_year(client, 2025, dest_dir=tmp_path / "out", tmp_dir=tmp_path / "tmp")
    assert result.path.read_bytes() == payload
    (request,) = posts
    assert "f5_session=abc" in request.headers.get("cookie", "")
    body = parse_qs(request.content.decode("ascii"))
    assert body["__VIEWSTATE"] == ["dGVzdA+/viewstate=="]
    assert body["cboYear"] == ["2025"] and body["cboPeriod"] == ["All"] and body["cboGeography"] == ["All"]
    assert all(body[field] == ["on"] for field in t100.FIELDS)
    assert "ORIGIN_CITY_NAME" not in body  # only requested fields are checked
