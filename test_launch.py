"""
Quick test script to generate a JWT and open the /lof endpoint.

Usage:
    python test_launch.py              # ESign flow (default)
    python test_launch.py --legacy     # Legacy flow (sub + file_id)

Requirements:
    pip install python-jose httpx
"""

import sys
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from jose import jwt

# ── Config ────────────────────────────────────────────────────────────────────
# Must match INTEGRATION_SHARED_SECRET in your .env
SHARED_SECRET = "ZXNpZ24ubXljcGFkZXNrLmNvbUAyMDI2"

# Your running frontend URL
FRONTEND_URL = "http://localhost:5173"

# ── ESign flow (new) ──────────────────────────────────────────────────────────
# Set these to real values from the CpaDesk ESignRequests table.
ESIGN_REQUEST_ID = 1          # ESignRequestID=1 → admin=1219 (ABJ Admin), signer=1262 (karan client)
LOGIN_TOKEN      = ""         # LoginDetail.Token for the role being launched (leave empty to skip verification)
# NOTE: role is NOT needed in the ESign JWT — it is read from ESignRequests.AssignedRole in the DB.

# ── Legacy flow (old) ─────────────────────────────────────────────────────────
LOGIN_DETAIL_ID = "31"        # sub – LoginDetailID=31 (Brijesh → ADMIN)
FILE_ID         = "2"         # file_id – FileID from FileMaster
LEGACY_ROLE     = "admin"     # "admin" for CPAUser, "user" for Client
DOCUMENT_PATH   = "https://cpaapi.newtechtest.in/CPADeskDocumentUpload/file-example_PDF_500_kB.pdf"

# ─────────────────────────────────────────────────────────────────────────────

use_legacy = "--legacy" in sys.argv

if use_legacy:
    payload = {
        "sub":           LOGIN_DETAIL_ID,
        "role":          LEGACY_ROLE,
        "file_id":       FILE_ID,
        "document_path": DOCUMENT_PATH,
        "token":         "",
        "jti":           str(uuid4()),
        "exp":           int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    print("\n[Legacy flow] Generating token with sub + file_id ...")
else:
    payload = {
        "eSignRequestId": ESIGN_REQUEST_ID,
        "token":          LOGIN_TOKEN,
        "jti":            str(uuid4()),
        "exp":            int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    print(f"\n[ESign flow] Generating token for ESignRequestID={ESIGN_REQUEST_ID} ...")

token = jwt.encode(payload, SHARED_SECRET, algorithm="HS256")

print(f"\nOpen this URL in your browser:\n{FRONTEND_URL}/lof?token={token}\n")
print("Note: each token is single-use. Run the script again for a new token.\n")
