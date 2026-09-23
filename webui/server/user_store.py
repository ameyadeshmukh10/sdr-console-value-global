"""Console logins — the email + password accounts an admin creates at runtime.

The app ships with a small set of BUILT-IN accounts hard-coded in app.py
(``_USERS``). This store holds the ones created from the Admin view: it lives on
the Railway volume at ``data/outreach/users.json`` (gitignored, so it survives
redeploys and never lands in git). ``USER_STORE_PATH`` overrides the location
for tests.

Passwords are never stored. Each record keeps a random 16-byte salt and the
PBKDF2-HMAC-SHA256 digest at 240,000 iterations — the same parameters as the
built-in table, so both verify through identical code. Roles are ``admin``
(may manage logins) or ``member`` (everything else the console does).

Built-in accounts are read-only here: they are defined in code, so the store
refuses to create, shadow, or delete one (``reserved``). That keeps a bad edit
from locking every admin out of the console.

stdlib only; no I/O at import.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

ITERATIONS = 240000
MIN_PASSWORD = 10
MAX_PASSWORD = 256          # PBKDF2 has no practical cap; this bounds the work
MAX_EMAIL = 190
ROLES = ("admin", "member")

# Deliberately permissive: the real check is that a person can sign in with it.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_LOCK = threading.Lock()


class UserError(ValueError):
    """A rejected admin action. ``code`` is the HTTP status the API should send."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_path(data_dir):
    """users.json on the volume, unless USER_STORE_PATH overrides it."""
    override = (os.environ.get("USER_STORE_PATH") or "").strip()
    return Path(override) if override else Path(data_dir) / "outreach" / "users.json"


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def hash_password(password, salt_hex=None):
    """(salt_hex, hash_hex) for a password; a fresh random salt unless given."""
    salt_hex = salt_hex or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex), ITERATIONS)
    return salt_hex, dk.hex()


def check_password(password, salt_hex, hash_hex):
    """Constant-time password check against a stored (salt, digest) pair."""
    try:
        _, candidate = hash_password(password, salt_hex)
    except ValueError:            # malformed salt in the file — never a match
        return False
    return hmac.compare_digest(candidate, hash_hex or "")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def normalize_email(email):
    return (email or "").strip().lower()


def validate_email(email):
    email = normalize_email(email)
    if not email:
        raise UserError("an email address is required")
    if len(email) > MAX_EMAIL:
        raise UserError(f"that email is too long (max {MAX_EMAIL} characters)")
    if not _EMAIL_RE.match(email):
        raise UserError("that doesn't look like an email address")
    return email


def validate_password(password):
    password = password or ""
    if len(password) < MIN_PASSWORD:
        raise UserError(f"the password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"the password must be at most {MAX_PASSWORD} characters")
    if password.strip() != password:
        raise UserError("the password cannot start or end with whitespace")
    return password


def validate_role(role):
    role = (role or "member").strip().lower()
    if role not in ROLES:
        raise UserError(f"role must be one of: {', '.join(ROLES)}")
    return role


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class UserStore:
    """JSON-file account store. Reads never raise; writes are atomic and locked.

    ``reserved`` is the set of built-in emails (app.py's ``_USERS``) that this
    store must never shadow or claim to own.
    """

    def __init__(self, path, reserved=()):
        self.path = Path(path)
        self.reserved = {normalize_email(e) for e in reserved}

    # -- file I/O ----------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("users"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "users": []}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), "utf-8")
        try:                       # 0600: the digests are secrets at rest
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # -- reads -------------------------------------------------------------
    @staticmethod
    def _public(rec):
        """A record without the salt/digest — safe to hand to the browser."""
        return {
            "email": rec.get("email", ""),
            "role": rec.get("role", "member"),
            "created_at": rec.get("created_at"),
            "created_by": rec.get("created_by"),
            "password_changed_at": rec.get("password_changed_at"),
            "builtin": False,
        }

    def list(self):
        return sorted((self._public(u) for u in self._load()["users"]),
                      key=lambda u: u["email"])

    def get(self, email):
        """The raw record (salt + digest included) for an email, or None."""
        email = normalize_email(email)
        for rec in self._load()["users"]:
            if normalize_email(rec.get("email")) == email:
                return rec
        return None

    def verify(self, email, password):
        """The public record iff the password matches, else None."""
        rec = self.get(email)
        if not rec or not check_password(password, rec.get("salt", ""), rec.get("hash", "")):
            return None
        return self._public(rec)

    def role_of(self, email):
        rec = self.get(email)
        return rec.get("role", "member") if rec else None

    # -- writes ------------------------------------------------------------
    def create(self, email, password, role="member", by=None):
        email = validate_email(email)
        password = validate_password(password)
        role = validate_role(role)
        if email in self.reserved:
            raise UserError("that login is built into the app and can't be re-created", 409)
        with _LOCK:
            data = self._load()
            if any(normalize_email(u.get("email")) == email for u in data["users"]):
                raise UserError("a login with that email already exists", 409)
            salt_hex, hash_hex = hash_password(password)
            rec = {"email": email, "salt": salt_hex, "hash": hash_hex, "role": role,
                   "created_at": now_iso(), "created_by": by or None,
                   "password_changed_at": now_iso()}
            data["users"].append(rec)
            self._save(data)
        return self._public(rec)

    def _mutate(self, email, apply_):
        email = validate_email(email)
        with _LOCK:
            data = self._load()
            rec = next((u for u in data["users"]
                        if normalize_email(u.get("email")) == email), None)
            if rec is None:
                raise UserError("no such login", 404)
            apply_(rec)
            self._save(data)
        return self._public(rec)

    def set_password(self, email, password, by=None):
        password = validate_password(password)

        def apply_(rec):
            rec["salt"], rec["hash"] = hash_password(password)
            rec["password_changed_at"] = now_iso()
            rec["password_changed_by"] = by or None
        return self._mutate(email, apply_)

    def set_role(self, email, role, by=None):
        role = validate_role(role)

        def apply_(rec):
            rec["role"] = role
            rec["role_changed_at"] = now_iso()
            rec["role_changed_by"] = by or None
        return self._mutate(email, apply_)

    def delete(self, email):
        email = validate_email(email)
        with _LOCK:
            data = self._load()
            kept = [u for u in data["users"]
                    if normalize_email(u.get("email")) != email]
            if len(kept) == len(data["users"]):
                raise UserError("no such login", 404)
            data["users"] = kept
            self._save(data)
        return True


# --------------------------------------------------------------------------
# offline self-test — python3 webui/server/user_store.py --self-test
# --------------------------------------------------------------------------

def _self_test():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = UserStore(Path(tmp) / "users.json", reserved={"builtin@everworker.ai"})
        assert store.list() == [], "a missing file reads as no users"
        assert store.verify("nobody@x.com", "whatever") is None

        rec = store.create("New.User@Example.com", "correct horse battery", "member", by="admin@x.com")
        assert rec["email"] == "new.user@example.com", rec
        assert rec["role"] == "member" and rec["builtin"] is False
        assert "hash" not in rec and "salt" not in rec, "public records leak no digest"

        raw = json.loads((Path(tmp) / "users.json").read_text("utf-8"))
        assert "correct horse battery" not in json.dumps(raw), "passwords are never stored"

        assert store.verify("NEW.USER@example.com", "correct horse battery")  # case-insensitive
        assert store.verify("new.user@example.com", "wrong password") is None

        for bad, why in (("nope", "too short"), ("  leading space pw", "whitespace")):
            try:
                store.create("other@example.com", bad)
                raise AssertionError(f"accepted a {why} password")
            except UserError:
                pass
        for bad in ("builtin@everworker.ai", "not-an-email", "new.user@example.com"):
            try:
                store.create(bad, "a long enough password")
                raise AssertionError(f"accepted {bad!r}")
            except UserError:
                pass

        store.set_password("new.user@example.com", "a different long password", by="admin@x.com")
        assert store.verify("new.user@example.com", "correct horse battery") is None
        assert store.verify("new.user@example.com", "a different long password")

        assert store.set_role("new.user@example.com", "admin")["role"] == "admin"
        assert store.role_of("new.user@example.com") == "admin"
        try:
            store.set_role("new.user@example.com", "superuser")
            raise AssertionError("accepted an unknown role")
        except UserError:
            pass

        assert store.delete("new.user@example.com") is True
        assert store.list() == [] and store.role_of("new.user@example.com") is None
        for missing in (lambda: store.delete("new.user@example.com"),
                        lambda: store.set_password("new.user@example.com", "a long password")):
            try:
                missing()
                raise AssertionError("acted on a deleted login")
            except UserError as e:
                assert e.code == 404, e.code

        # A corrupt/hand-edited file degrades to "no users", never a crash.
        (Path(tmp) / "users.json").write_text("{not json", "utf-8")
        assert store.list() == [] and store.verify("a@b.com", "x") is None
    print("user_store self-test OK")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
    else:
        print(__doc__)
