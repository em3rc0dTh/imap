# from .ingest_email import connect_and_download_pdfs
# from .config import IMAP_LIMIT
# import argparse
# import json

# def main(limit=None, force=False):
#     # If limit is None we let ingest decide based on config (IMAP_LIMIT)
#     res = connect_and_download_pdfs(limit=limit, force=force)
#     print(f"Procesados: {len(res)} mensajes")
#     # Print summary
#     for item in res:
#         uid = item["uid"]
#         md = item["metadata"]
#         print(f"- UID {uid} | subject: {md.get('subject')} | pdfs: {md.get('pdfs')}")

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description="IMAP PDF downloader pipeline")
#     parser.add_argument("--limit", type=int, default=None,
#                         help="Limit number of messages to fetch (0 = all / default uses IMAP_LIMIT if set)")
#     parser.add_argument("--force", action="store_true", help="Force reprocess messages even if already processed")
#     args = parser.parse_args()
#     main(limit=args.limit, force=args.force)
from .ingest_email import connect_and_download_pdfs
from .config import IMAP_LIMIT
import argparse

def main(limit=None, force=False):
    res = connect_and_download_pdfs(limit=limit, force=force)
    print(f"Procesados: {len(res)} mensajes")
    for item in res:
        uid = item["uid"]
        md = item["metadata"]
        print(f"- UID {uid} | subject: {md.get('subject')} | pdfs: {md.get('pdfs')}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IMAP PDF downloader pipeline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    main(limit=args.limit, force=args.force)
