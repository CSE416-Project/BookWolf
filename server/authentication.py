"""Authentication and authorization for CampusReserve."""

from datetime import datetime, timedelta, timezone
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import ValidationError
from sqlalchemy.orm import Session

from database import get_db
from models.user import User
from access_control.scopes import SCOPES
from access_control.roles import ROLES

SECRET_KEY = os.environ["JWT_SECRET_KEY"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# --- Password hashing (NFR-3) ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# --- OAuth2 scheme, wired to the scope catalog so it shows in the API docs ---
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="token",
    scopes={name: meta["description"] for name, meta in SCOPES.items()},
)


# --- Token creation ---
def create_access_token(user: User) -> str:
    """Issue a signed JWT carrying the user's id and their role's scopes."""
    scopes = scopes_for_user(user)
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),  # subject = user id
        "scopes": scopes,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def scopes_for_user(user: User) -> list[str]:
    """Expand a user's single role into its granted scopes."""
    return ROLES.get(user.role.value, [])


# --- Authentication helpers ---
def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Return the user if the email exists and the password matches, else None."""
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


# --- The dependency endpoints use to require scopes ---
# To protect an action, declare it with:
#     user: User = Security(get_current_user, scopes=[...])
# where scopes lists the scope(s) that action requires (e.g. ["handle:requests"]).
# get_current_user then authenticates the caller AND checks they hold every listed scope.
# Use Security(...), not Depends(...), for scoped endpoints — scopes only get
# passed through when you use Security.
async def get_current_user(
    security_scopes: SecurityScopes,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    # The WWW-Authenticate header names the required scopes on failure.
    if security_scopes.scopes:
        authenticate_value = f'Bearer scope="{security_scopes.scope_str}"'
    else:
        authenticate_value = "Bearer"

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": authenticate_value},
    )

    # 1. Decode and validate the token.
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        token_scopes = payload.get("scopes", [])
        if user_id is None:
            raise credentials_exception
    except (JWTError, ValidationError):
        raise credentials_exception

    # 2. Load the user.
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user"
        )

    # 3. Enforce that every scope the endpoint requires is present.
    for scope in security_scopes.scopes:
        if scope not in token_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions",
                headers={"WWW-Authenticate": authenticate_value},
            )

    return user
