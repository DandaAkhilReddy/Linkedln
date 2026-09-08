"""
Storage backend abstraction.

Every module talks to a "store" with the Azure Blob container-client shape:
    store.download_blob(name).readall() -> bytes
    store.upload_blob(name, data, overwrite=True)
    store.list_blobs(name_starts_with=None) -> iterable of objects with .name
    store.delete_blob(name)

Two backends, chosen by STORAGE_BACKEND (or auto-detected):
    azure  -> Azure Blob Storage (AzureWebJobsStorage / AZURE_STORAGE_CONNECTION_STRING)
    file   -> local directory DATA_DIR/<container>/  (Railway volume, VM, laptop)

Moving hosts therefore never touches business logic: set STORAGE_BACKEND=file
and a persistent DATA_DIR, copy the JSON/PNG blobs over, done.
"""

import os
import io
import tempfile


class _Downloaded:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class _Entry:
    def __init__(self, name):
        self.name = name


class FileStore:
    """Filesystem-backed store with the same surface as a Blob container client."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, name):
        p = os.path.normpath(os.path.join(self.root, name))
        if not p.startswith(os.path.normpath(self.root)):
            raise ValueError(f"unsafe blob name: {name}")
        return p

    def create_container(self):
        os.makedirs(self.root, exist_ok=True)

    def download_blob(self, name):
        p = self._path(name)
        if not os.path.isfile(p):
            raise FileNotFoundError(name)
        with open(p, "rb") as f:
            return _Downloaded(f.read())

    def upload_blob(self, name, data, overwrite=True):
        p = self._path(name)
        if os.path.exists(p) and not overwrite:
            raise FileExistsError(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif hasattr(data, "read"):
            data = data.read()
        # atomic write so a crash mid-write never corrupts queue/state files
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(data))
        os.replace(tmp, p)

    def list_blobs(self, name_starts_with=None):
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                if fn.startswith(".tmp-"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), self.root).replace(os.sep, "/")
                if not name_starts_with or rel.startswith(name_starts_with):
                    yield _Entry(rel)

    def delete_blob(self, name):
        p = self._path(name)
        if os.path.isfile(p):
            os.remove(p)


def backend():
    b = (os.getenv("STORAGE_BACKEND") or "").lower()
    if b in ("azure", "file"):
        return b
    return "azure" if (os.getenv("AzureWebJobsStorage") or
                       os.getenv("AZURE_STORAGE_CONNECTION_STRING")) else "file"


def get_store(container):
    """Return a store for a logical container ('linkedin-posts', 'linkedin-logos')."""
    if backend() == "azure":
        from azure.storage.blob import BlobServiceClient
        conn = os.getenv("AzureWebJobsStorage") or os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        c = BlobServiceClient.from_connection_string(conn).get_container_client(container)
        try:
            c.create_container()
        except Exception:
            pass
        return c
    root = os.path.join(os.getenv("DATA_DIR", "./data"), container)
    return FileStore(root)
