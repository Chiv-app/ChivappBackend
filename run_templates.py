from app.db.session import SessionLocal
from app.services.email.service import ensure_email_templates

db = SessionLocal()
try:
    ensure_email_templates(db, update_existing=True)
    print('Templates updated successfully')
finally:
    db.close()
