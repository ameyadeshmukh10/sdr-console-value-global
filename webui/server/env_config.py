"""The console's .env handling (stdlib only, no python-dotenv).

ENV_PATH is the project-root .env: your own file in local dev, the baked-in
.env.defaults in the Docker image (see Dockerfile). Real environment variables
(Railway service variables) always win over it.

  load_env_file()  — boot: copy ENV_PATH into os.environ, setdefault-style
  log_settings()   — boot: print every setting + its source, secrets masked
  read_env()       — merged config dict, file re-read live on every call
"""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"

# Keys load_env_file() copied from ENV_PATH into os.environ (i.e. NOT host-provided).
_DOTENV_KEYS: set[str] = set()

_SECRET_KEY_RE = re.compile(r"(_KEY|SECRET|TOKEN|PASSWORD|MONGO_URL)$|SECRET|PASSWORD")
_ENV_EXAMPLE_KEY_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]+)=", re.M)


def load_env_file() -> None:
    """Boot: copy ENV_PATH into os.environ (real env wins) so loops reading
    os.environ directly see it."""
    for key, val in _parse_env_file().items():
        if key not in os.environ:
            os.environ[key] = val
            _DOTENV_KEYS.add(key)


def log_settings() -> None:
    """Boot: print each setting with its source (env / .env / unset), secrets masked."""
    example = PROJECT_ROOT / ".env.example"
    documented: set[str] = set(_ENV_EXAMPLE_KEY_RE.findall(example.read_text())) if example.is_file() else set()
    file_keys = set(_parse_env_file())
    prefixes = tuple(sorted({k.split("_", 1)[0] + "_" for k in documented if "_" in k}))
    keys = documented | file_keys | {k for k in os.environ if k.startswith(prefixes)}
    width = max((len(k) for k in keys), default=0)
    unset: list[str] = []
    print(f"[config] settings ({ENV_PATH} {'present' if ENV_PATH.is_file() else 'absent'}):", flush=True)
    for key in sorted(keys):
        if key not in os.environ:
            unset.append(key)
            continue
        if key in _DOTENV_KEYS:
            src = ".env"
        elif key in file_keys:
            src = "env (overrides .env)"
        else:
            src = "env"
        print(f"[config]   {key:<{width}} = {_mask_setting(key, os.environ[key])}  [{src}]", flush=True)
    if unset:
        print(f"[config] unset (code defaults): {', '.join(unset)}", flush=True)


def read_env() -> dict[str, str]:
    """Merged config: the .env file, re-read on every call, overlaid by real env vars
    (host wins; boot-loaded copies don't shadow later file edits)."""
    env = _parse_env_file()
    env.update({k: v for k, v in os.environ.items() if k not in _DOTENV_KEYS})
    return env


def _parse_env_file() -> dict[str, str]:
    """KEY=value pairs from ENV_PATH ({} when absent)."""
    env: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # strip inline comments + surrounding quotes/whitespace
            val = val.split("#", 1)[0].strip().strip('"').strip("'")
            env[key.strip()] = val
    return env


def _mask_setting(key: str, val: str) -> str:
    if val and _SECRET_KEY_RE.search(key):
        return f"<set, {len(val)} chars>"
    return re.sub(r"//[^/@\s]+@", "//***@", val)  # credentials inside a URL
