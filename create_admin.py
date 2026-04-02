"""One-time script to create an admin user."""
import hashlib, uuid
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from r2_storage import get_user_by_email, upsert_user

EMAIL = "admin@gmail.com"
PASSWORD = "admin123"

existing = get_user_by_email(EMAIL)
if existing:
    print(f"User {EMAIL} already exists (id={existing['id']})")
else:
    user = {
        "id": str(uuid.uuid4()),
        "email": EMAIL,
        "display_name": "Admin",
        "password_hash": hashlib.sha256(PASSWORD.encode()).hexdigest(),
        "role": "admin",
        "groups": [],
        "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "invited_by": None,
    }
    upsert_user(user)
    print(f"Admin user created: {EMAIL}")
