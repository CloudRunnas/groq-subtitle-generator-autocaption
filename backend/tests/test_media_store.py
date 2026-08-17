"""Unit tests for media store helpers and S3 GET presign."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.media_store import MediaStore, result_download_filename


def test_result_download_filename():
    assert result_download_filename("clip.mp4") == "clip_subtitled.mp4"
    assert result_download_filename("a.b.mov") == "a.b_subtitled.mov"
    assert result_download_filename("noext") == "noext_subtitled.mp4"
    assert result_download_filename(None) == "video_subtitled.mp4"


def test_presign_get_uses_12h_ttl_and_attachment():
    class Settings:
        media_bucket = "media-test"
        aws_region = "eu-central-1"
        media_local_dir = "/tmp/autocaption-media-test"

    store = MediaStore(Settings())
    mock = MagicMock()
    mock.generate_presigned_url.return_value = "https://media-test.s3.amazonaws.com/jobs/j1/output/result.mp4?X-Amz-Expires=43200"
    store._s3 = mock
    store.bucket = "media-test"

    url = store.presign_get(
        "jobs/j1/output/result.mp4",
        expires=12 * 60 * 60,
        response_content_disposition='attachment; filename="clip_subtitled.mp4"',
        response_content_type="video/mp4",
    )
    assert "X-Amz-Expires=43200" in url
    mock.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={
            "Bucket": "media-test",
            "Key": "jobs/j1/output/result.mp4",
            "ResponseContentDisposition": 'attachment; filename="clip_subtitled.mp4"',
            "ResponseContentType": "video/mp4",
        },
        ExpiresIn=43200,
    )


def test_presign_get_none_without_s3():
    class Settings:
        media_bucket = ""
        aws_region = "eu-central-1"
        media_local_dir = "/tmp/autocaption-media-test"

    store = MediaStore(Settings())
    assert store.presign_get("jobs/j1/output/result.mp4") is None


def test_download_file_copies_local(tmp_path):
    class Settings:
        media_bucket = ""
        aws_region = "eu-central-1"
        media_local_dir = str(tmp_path / "media")

    store = MediaStore(Settings())
    key = "jobs/j1/input/v.mp4"
    store.put_bytes(key, b"video-bytes", "video/mp4")
    dest = tmp_path / "out.mp4"
    store.download_file(key, dest)
    assert dest.read_bytes() == b"video-bytes"
