"""The editor's image routes over a fake storage backend (MEDIA-001, ADR-062).

Nothing here reaches Cloudinary: the backend is faked at ``get_storage_backend``
where each router imports it, and the fake records every upload and deletion
so a test can read what would have been stored and where.
"""
import os
from unittest.mock import patch

import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeStorage:
    """Records what the routes ask of storage and answers like Cloudinary would."""

    def __init__(self):
        self.uploads: list = []
        self.deleted: list = []

    async def upload(self, file_content: bytes, filename: str, folder: str = "", **options) -> dict:
        public_id = f"{folder}/img{len(self.uploads) + 1}"
        self.uploads.append({"folder": folder, "filename": filename, "bytes": len(file_content), "options": options})
        return {"url": f"http://cdn.test/{public_id}.png", "secure_url": f"https://cdn.test/{public_id}.png",
                "public_id": public_id, "width": 640, "height": 480, "format": "png", "bytes": len(file_content)}

    async def delete(self, public_id: str) -> bool:
        self.deleted.append(public_id)
        return True

    def get_thumbnail_url(self, public_id: str, width: int = 200, height: int = 200) -> str:
        return f"https://cdn.test/thumb/{public_id}"


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana")
        w.act_as(uid)
        w.uid = uid
        yield w


@pytest.fixture
def storage():
    fake = FakeStorage()
    with patch("app.routers.image_upload.get_storage_backend", return_value=fake):
        yield fake


def _upload(world, content=PNG, name="pic.png", content_type="image/png", **form):
    return world.http.post("/image/upload", files={"file": (name, content, content_type)}, data=form)


# ── MEDIA-001: the root folder is the environment's ─────────────────────────

def test_uploads_and_the_delete_guard_use_the_environment_root(world, storage):
    user = world.current["user"]["user_id"]
    with patch.dict(os.environ, {"MEDIA_ROOT_FOLDER": "nowry-test"}):
        response = _upload(world)
        assert response.status_code == 200, response.text
        assert storage.uploads[0]["folder"] == f"nowry-test/{user}"
        assert world.http.delete(f"/image/delete/nowry/{user}/img1").status_code == 403
        assert world.http.delete(f"/image/delete/nowry-test/{user}/img1").status_code == 200
    assert storage.deleted == [f"nowry-test/{user}/img1"]


def test_without_the_variable_the_root_is_nowry(world, storage):
    user = world.current["user"]["user_id"]
    with patch.dict(os.environ, {"MEDIA_ROOT_FOLDER": ""}):
        response = _upload(world)
        assert response.status_code == 200, response.text
        assert storage.uploads[0]["folder"] == f"nowry/{user}"
        assert world.http.delete(f"/image/delete/nowry/{user}/img1").status_code == 200
        assert world.http.delete(f"/image/delete/nowry/{world.new_user('bob')}/img1").status_code == 403
