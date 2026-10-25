"""Create or update an admin user for the Chivapp platform."""

import sys
from datetime import datetime

from app.core.hashing import hash_password
from app.db.session import SessionLocal
from app.models.user import User, UserRole


def main() -> None:
    if len(sys.argv) < 4:
        print("Uso: python -m app.cli.create_admin <email> <fullname> <password>")
        sys.exit(1)

    email, fullname, password = sys.argv[1:4]
    db = SessionLocal()

    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            existing.role = UserRole.admin
            existing.password_hash = hash_password(password)
            existing.fullname = fullname
            existing.is_verified = True
            existing.email_verified_at = datetime.utcnow()
            db.commit()
            print(f"Usuario admin actualizado: {email}")
            return

        user = User(
            email=email,
            fullname=fullname,
            password_hash=hash_password(password),
            role=UserRole.admin,
            is_verified=True,
            email_verified_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        print(f"Usuario admin creado: {email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
