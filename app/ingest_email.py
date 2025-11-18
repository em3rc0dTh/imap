import os
from imapclient import IMAPClient
import pyzmail
from .config import IMAP_HOST, IMAP_USER, IMAP_PASS, IMAP_FOLDER, PDF_SAVE_DIR, IMAP_SENDER_FILTER

os.makedirs(PDF_SAVE_DIR, exist_ok=True)

def connect_and_download_pdfs(limit=10):
    saved = []
    with IMAPClient(IMAP_HOST) as client:
        client.login(IMAP_USER, IMAP_PASS)
        client.select_folder(IMAP_FOLDER)
        criteria = ['FROM', IMAP_SENDER_FILTER] if IMAP_SENDER_FILTER else ['ALL']
        messages = client.search(criteria)[-limit:]
        resp = client.fetch(messages, ['BODY[]'])
        for uid, data in resp.items():
            msg = pyzmail.PyzMessage.factory(data[b'BODY[]'])
            for part in msg.mailparts:
                if part.filename and part.filename.lower().endswith(".pdf"):
                    path = os.path.join(PDF_SAVE_DIR, f"{uid}_{part.filename}")
                    with open(path, "wb") as f:
                        f.write(part.get_payload())
                    saved.append(path)
    return saved
