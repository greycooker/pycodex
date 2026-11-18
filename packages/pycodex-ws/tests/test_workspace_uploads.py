from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from workspace_server.uploads import MAX_UPLOAD_BYTES, store_upload
from test_session_api import api_setup, session_base


def test_upload_routes_save_files_without_submitting_messages(api_setup):
    app, prefix = api_setup()
    with TestClient(app) as client:
        base = session_base(client, prefix)
        assert client.get(prefix + "/api/uploads").json()["max_file_bytes"] == 256 * 1024 * 1024
        filename = "报告.txt"
        headers = {"content-type": "application/octet-stream",
                   "x-upload-filename": quote(filename)}
        first = client.post(base + "/uploads", content=b"file contents", headers=headers)
        assert first.status_code == 201
        saved = first.json()
        assert saved["name"] == filename and saved["bytes"] == 13
        assert Path(saved["path"]).read_bytes() == b"file contents"
        second = client.post(base + "/uploads", content=b"different", headers=headers).json()
        assert second["path"] != saved["path"]
        assert Path(saved["path"]).read_bytes() == b"file contents"
        assert client.get(base + "/history").json()["items"] == []
        assert client.post(base + "/uploads", content=b"x", headers={
            **headers, "content-length": str(MAX_UPLOAD_BYTES + 1),
        }).status_code == 413
        for name in ("../outside", "/absolute", "a\\b", "line\nbreak", ""):
            assert client.post(base + "/uploads", content=b"x", headers={
                **headers, "x-upload-filename": quote(name),
            }).status_code == 422
        assert client.post(prefix + "/api/sessions/missing/uploads", content=b"x",
                           headers=headers).status_code == 404
        assert client.post(base + "/uploads", content=b"x").status_code == 415


def test_upload_routes_require_workspace_authentication(api_setup):
    app, prefix = api_setup(password="test-password")
    with TestClient(app) as client:
        assert client.get(prefix + "/api/uploads").status_code == 401
        assert client.post(prefix + "/api/sessions/id/uploads", content=b"x",
                           headers={"content-type": "application/octet-stream",
                                    "x-upload-filename": "file.txt"}).status_code == 401


def request_from_chunks(chunks, length=None, disconnect=False):
    iterator = iter(chunks)
    async def receive():
        try:
            return {"type": "http.request", "body": next(iterator), "more_body": True}
        except StopIteration:
            if disconnect:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}
    headers = [] if length is None else [(b"content-length", str(length).encode())]
    return Request({"type": "http", "headers": headers}, receive)


@pytest.mark.asyncio
async def test_stream_limits_and_interruption_remove_partial_files(tmp_path):
    request = request_from_chunks([b"1234", b"5678"])
    with pytest.raises(HTTPException) as error:
        await store_upload(request, tmp_path, "file.bin", max_bytes=7)
    assert error.value.status_code == 413
    assert list(tmp_path.iterdir()) == []
    request = request_from_chunks([b"partial"], disconnect=True)
    with pytest.raises(Exception):
        await store_upload(request, tmp_path, "file.bin")
    assert list(tmp_path.iterdir()) == []
    request = request_from_chunks([b"short"], length=10)
    with pytest.raises(HTTPException) as error:
        await store_upload(request, tmp_path, "file.bin")
    assert error.value.status_code == 400
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_exact_256_mb_upload_is_accepted_with_bounded_chunks(tmp_path):
    chunk = b"test" * (1024 * 1024 // 4)
    result = await store_upload(request_from_chunks(
        (chunk for _ in range(256)), length=MAX_UPLOAD_BYTES,
    ), tmp_path, "boundary.bin")
    path = Path(result["path"])
    assert result["bytes"] == path.stat().st_size == MAX_UPLOAD_BYTES
    with path.open("rb") as handle:
        assert handle.read(1024) == chunk[:1024]
        handle.seek(-1024, 2)
        assert handle.read() == chunk[-1024:]
    path.unlink()
    path.parent.rmdir()
