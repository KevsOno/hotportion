"""
Pytest configuration. Sets the minimum env vars required by `main.Settings`
BEFORE `main` is imported, so module-level instantiation doesn't explode.

Run with:  pytest -q
"""
import os
import sys
from pathlib import Path

# ---- Required settings (dummies — no network calls happen at import time) ----
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-role-key")
os.environ.setdefault("BREVO_API_KEY", "test-brevo-key")
os.environ.setdefault("BREVO_SENDER_EMAIL", "no-reply@example.com")
os.environ.setdefault("MONNIFY_API_KEY", "test-monnify-api-key")
os.environ.setdefault("MONNIFY_SECRET_KEY", "test-monnify-secret-key")
os.environ.setdefault("MONNIFY_CONTRACT_CODE", "test-contract-code")
# Ensure reconciliation loop never starts during tests
os.environ.setdefault("RECONCILIATION_ENABLED", "false")

# Make the project root importable so `import main` works
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
