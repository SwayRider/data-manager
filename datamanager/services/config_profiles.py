from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from datamanager.errors import ValidationError
from datamanager.models import ConfigProfile

NAME_MAX = 100


def list_profiles(session: Session) -> list[ConfigProfile]:
    return list(session.scalars(select(ConfigProfile).order_by(ConfigProfile.name)))


def get_profile(session: Session, profile_id: int) -> ConfigProfile | None:
    return session.get(ConfigProfile, profile_id)


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(ConfigProfile.id).where(ConfigProfile.name == name)  # column is NOCASE
    if exclude_id is not None:
        stmt = stmt.where(ConfigProfile.id != exclude_id)
    return session.scalar(stmt) is not None


def _clean_name(session: Session, name: str, exclude_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Name is required", field="name")
    if len(name) > NAME_MAX:
        raise ValidationError(f"Name must be at most {NAME_MAX} characters", field="name")
    if _name_taken(session, name, exclude_id):
        raise ValidationError("A configuration with this name already exists", field="name")
    return name


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:  # lost a race with another writer on the unique name
        session.rollback()
        raise ValidationError("A configuration with this name already exists", field="name")


def create_profile(session: Session, name: str, description: str = "") -> ConfigProfile:
    profile = ConfigProfile(name=_clean_name(session, name), description=(description or "").strip())
    session.add(profile)
    _commit(session)
    return profile


def update_profile(session: Session, profile: ConfigProfile, name: str, description: str) -> ConfigProfile:
    profile.name = _clean_name(session, name, exclude_id=profile.id)
    profile.description = (description or "").strip()
    _commit(session)
    return profile


def delete_profile(session: Session, profile: ConfigProfile) -> None:
    session.delete(profile)
    session.commit()


def duplicate_profile(session: Session, profile: ConfigProfile) -> ConfigProfile:
    """Copy a profile under a unique "<name> (copy[ n])" name."""
    base = profile.name
    candidate, n = f"{base} (copy)", 1
    while _name_taken(session, candidate):
        n += 1
        candidate = f"{base} (copy {n})"
    return create_profile(session, candidate, profile.description)
