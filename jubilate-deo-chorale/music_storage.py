"""File storage for the Music Library.

The database keeps the details of each file (title, category, composer, ...). The bytes themselves live here,
under a random key such as "3f9c...e1.pdf". Two places are supported, chosen with environment variables:

    MUSIC_STORAGE=local   (default)  files in a folder on this computer / server's disk
    MUSIC_STORAGE=s3                 any S3-compatible bucket: Amazon S3, Cloudflare R2, Backblaze B2, MinIO ...

Local disk (default folder: instance/uploads/music_library, or MUSIC_LOCAL_DIR) survives restarts on a normal
server or your own computer. It does NOT survive on serverless hosts such as Vercel, where the disk is
temporary. There, use MUSIC_STORAGE=s3. Settings for it:

    MUSIC_S3_BUCKET        (required) bucket name
    MUSIC_S3_PREFIX        folder inside the bucket, default "music-library/"
    MUSIC_S3_ENDPOINT_URL  needed for R2, B2, MinIO (leave empty for Amazon S3)
    MUSIC_S3_REGION        default "auto"
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   the bucket's access keys (standard names, read by boto3)

The bucket must be PRIVATE. The app never hands out bucket links: every download goes through the Flask routes,
which check who is signed in and what they may open. S3 mode needs the "boto3" package (pip install boto3).
"""

import os
import re
from pathlib import Path

KEY_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,4}$")
CHUNK = 64 * 1024


class StorageError(Exception):
    """The file could not be stored, read or removed."""


class StorageNotFound(StorageError):
    """The file is not in the storage (deleted by hand, or never arrived)."""


def _check_key(key):
    if not KEY_RE.match(key or ""):
        raise StorageError("bad storage key")


class LocalStorage:
    name = "local"

    def __init__(self, folder):
        self.folder = Path(folder)

    def put(self, key, data, content_type):
        _check_key(key)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.folder / (key + ".part")
            temp.write_bytes(data)
            temp.replace(self.folder / key)           # a half-written file is never visible under its real name
        except OSError as error:
            raise StorageError(str(error)) from error

    def read(self, key, start=0, end=None):
        """Yield the bytes of key from start to end (both inclusive; end None = to the end)."""
        _check_key(key)
        path = self.folder / key
        try:
            handle = path.open("rb")
        except FileNotFoundError as error:
            raise StorageNotFound(key) from error
        except OSError as error:
            raise StorageError(str(error)) from error

        def chunks():
            with handle:
                handle.seek(start)
                left = None if end is None else end - start + 1
                while left is None or left > 0:
                    block = handle.read(CHUNK if left is None else min(CHUNK, left))
                    if not block:
                        break
                    if left is not None:
                        left -= len(block)
                    yield block
        return chunks()

    def delete(self, key):
        _check_key(key)
        try:
            (self.folder / key).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise StorageError(str(error)) from error

    def exists(self, key):
        _check_key(key)
        return (self.folder / key).is_file()


class S3Storage:
    name = "s3"

    def __init__(self, bucket, prefix="music-library/", endpoint_url="", region="auto", client=None):
        if not bucket:
            raise StorageError("MUSIC_S3_BUCKET is not set")
        self.bucket = bucket
        self.prefix = prefix.lstrip("/")
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"
        self._client = client
        self._endpoint_url = endpoint_url or None
        self._region = region or "auto"

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3                       # only needed in S3 mode
            except ImportError as error:
                raise StorageError("MUSIC_STORAGE=s3 needs the boto3 package: pip install boto3") from error
            self._client = boto3.client("s3", endpoint_url=self._endpoint_url, region_name=self._region)
        return self._client

    def _key(self, key):
        _check_key(key)
        return self.prefix + key

    def put(self, key, data, content_type):
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type)
        except StorageError:
            raise
        except Exception as error:                 # botocore raises many kinds of error; none may leak details to users
            raise StorageError(str(error)) from error

    def read(self, key, start=0, end=None):
        args = {"Bucket": self.bucket, "Key": self._key(key)}
        if start or end is not None:
            args["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        try:
            body = self.client.get_object(**args)["Body"]
        except StorageError:
            raise
        except Exception as error:
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "404", "NotFound"):
                raise StorageNotFound(key) from error
            raise StorageError(str(error)) from error

        def chunks():
            try:
                yield from body.iter_chunks(CHUNK)
            finally:
                body.close()
        return chunks()

    def delete(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except StorageError:
            raise
        except Exception as error:
            raise StorageError(str(error)) from error

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except StorageError:
            raise
        except Exception:
            return False


def make_storage(default_local_folder):
    """Build the storage chosen by the environment (see the top of this file)."""
    kind = os.environ.get("MUSIC_STORAGE", "local").strip().lower()
    if kind == "s3":
        return S3Storage(
            bucket=os.environ.get("MUSIC_S3_BUCKET", "").strip(),
            prefix=os.environ.get("MUSIC_S3_PREFIX", "music-library/"),
            endpoint_url=os.environ.get("MUSIC_S3_ENDPOINT_URL", "").strip(),
            region=os.environ.get("MUSIC_S3_REGION", "auto").strip())
    if kind not in ("", "local"):
        raise StorageError(f"MUSIC_STORAGE must be 'local' or 's3', not '{kind}'")
    return LocalStorage(os.environ.get("MUSIC_LOCAL_DIR", "").strip() or default_local_folder)