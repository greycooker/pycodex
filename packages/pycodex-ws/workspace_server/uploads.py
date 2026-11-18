"""Stream browser files to workspace storage without embedding them in prompts."""

import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

MAX_UPLOAD_BYTES = 256 * 1024 * 1024


def upload_directory(manager):
    state = manager._state_store.path
    if state is not None:
        return state.parent / (state.stem + ".uploads")
    return manager.codex_home / "uploads"


def upload_name(value):
    name = unquote(value)
    if (not name or name in {".", ".."} or "/" in name or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or len(name.encode("utf-8")) > 240):
        raise HTTPException(422, "invalid upload filename")
    return name


def _create_file(root, name):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="file-", dir=str(root)))
    path = directory / name
    try:
        return directory, path, path.open("xb")
    except BaseException:
        shutil.rmtree(str(directory))
        raise


async def store_upload(request, root, name, max_bytes=MAX_UPLOAD_BYTES):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            length = int(length)
        except ValueError:
            raise HTTPException(400, "invalid content length")
        if length < 0:
            raise HTTPException(400, "invalid content length")
        if length > max_bytes:
            raise HTTPException(413, "file exceeds 256 MB upload limit")
    directory, path, handle = await run_in_threadpool(_create_file, root, name)
    complete = False
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, "file exceeds 256 MB upload limit")
            await run_in_threadpool(handle.write, chunk)
        if length is not None and size != length:
            raise HTTPException(400, "upload size does not match content length")
        await run_in_threadpool(handle.close)
        complete = True
        return {"name": name, "path": str(path.resolve()), "bytes": size}
    finally:
        if not handle.closed:
            handle.close()
        if not complete:
            shutil.rmtree(str(directory))


def upload_routes(workspace, prefix=""):
    async def capabilities(request):
        workspace(request)
        return JSONResponse({"max_file_bytes": MAX_UPLOAD_BYTES},
                            headers={"Cache-Control": "no-store"})

    async def upload(request):
        context = workspace(request)
        try:
            context.manager.get(request.path_params["session_id"])
        except KeyError:
            raise HTTPException(404, "session not found")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "upload body must be application/octet-stream")
        name = upload_name(request.headers.get("x-upload-filename", ""))
        try:
            result = await store_upload(request, upload_directory(context.manager), name)
        except OSError:
            raise HTTPException(507, "could not save upload to server storage")
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    return [
        Route(prefix + "/api/uploads", capabilities, methods=["GET"]),
        Route(prefix + "/api/sessions/{session_id}/uploads", upload, methods=["POST"]),
    ]
