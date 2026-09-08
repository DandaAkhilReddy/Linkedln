"""
Copy every blob (queue, state, secrets, logs, logos) between storage backends.
Used when moving hosts, e.g. Azure Blob -> a Railway volume:

    # 1) pull everything out of Azure into a local folder
    python migrate_storage.py --from-azure "<connection string>" --to-dir ./data

    # 2) push that folder to the new host's DATA_DIR (or mount it as the volume)
    #    ...or the reverse direction:
    python migrate_storage.py --from-dir ./data --to-azure "<connection string>"

Both logical containers are copied: linkedin-posts and linkedin-logos.
"""

import os
import sys
import argparse

from storage import FileStore

CONTAINERS = ("linkedin-posts", "linkedin-logos")


def azure_store(conn, container):
    from azure.storage.blob import BlobServiceClient
    c = BlobServiceClient.from_connection_string(conn).get_container_client(container)
    try:
        c.create_container()
    except Exception:
        pass
    return c


def copy(src, dst):
    n = 0
    for b in src.list_blobs():
        dst.upload_blob(b.name, src.download_blob(b.name).readall(), overwrite=True)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-azure", metavar="CONN")
    ap.add_argument("--from-dir", metavar="DIR")
    ap.add_argument("--to-azure", metavar="CONN")
    ap.add_argument("--to-dir", metavar="DIR")
    a = ap.parse_args()
    if not ((a.from_azure or a.from_dir) and (a.to_azure or a.to_dir)):
        ap.error("need one --from-* and one --to-*")
    for cont in CONTAINERS:
        src = azure_store(a.from_azure, cont) if a.from_azure else FileStore(os.path.join(a.from_dir, cont))
        dst = azure_store(a.to_azure, cont) if a.to_azure else FileStore(os.path.join(a.to_dir, cont))
        print(f"{cont}: copied {copy(src, dst)} blobs")


if __name__ == "__main__":
    sys.exit(main())
