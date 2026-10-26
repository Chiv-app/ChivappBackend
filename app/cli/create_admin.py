"""Create or update an admin user for the Chivapp platform."""

import argparse
import getpass
import os
import sys
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.hashing import hash_password
from app.db.session import SessionLocal
from app.models.user import User, UserRole


def upsert_admin_user(
    db: Session,
    *,
    email: str,
    fullname: str,
    password: str,
    promote: bool = False,
) -> User:
    if len(password or "") < 8:
        raise ValueError("La contraseña de administrador debe tener al menos 8 caracteres.")
    if len(password) > 128:
        raise ValueError("La contraseña de administrador no puede superar los 128 caracteres.")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        if existing.role != UserRole.admin and not promote:
            raise PermissionError(
                f"El correo {email} ya pertenece a una cuenta con rol '{existing.role.value}'. "
                "Usa --promote explícitamente si deseas promoverla a administrador."
            )
        existing.role = UserRole.admin
        existing.password_hash = hash_password(password)
        existing.fullname = fullname
        existing.is_verified = True
        existing.email_verified_at = datetime.utcnow()
        existing.token_version = (existing.token_version or 0) + 1
        db.commit()
        db.refresh(existing)
        return existing

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
    db.refresh(user)
    return user


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Crear o actualizar un usuario administrador (la contraseña se lee de ADMIN_PASSWORD o getpass)."
    )
    parser.add_argument("email", help="Correo del administrador")
    parser.add_argument("fullname", help="Nombre completo del administrador")
    parser.add_argument(
        "password",
        nargs="?",
        default=None,
        help="Contraseña (opcional; preferir variable ADMIN_PASSWORD o entrada interactiva)",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Permitir promover una cuenta existente de músico o contratista a admin",
    )

    args = parser.parse_args(argv)
    password = args.password or os.environ.get("ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass("Contraseña de administrador: ")

    db = SessionLocal()
    try:
        try:
            upsert_admin_user(
                db,
                email=args.email,
                fullname=args.fullname,
                password=password,
                promote=args.promote,
            )
        except (ValueError, PermissionError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Usuario admin configurado: {args.email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
