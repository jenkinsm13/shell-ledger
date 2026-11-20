"""Project paths and the frozen rules. The rules are read, never written."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class FrozenError(RuntimeError):
    pass


def home() -> Path:
    env = os.environ.get("SHELL_LEDGER_HOME")
    p = Path(env) if env else Path.cwd()
    for q in [p, *p.parents]:
        if (q / "frozen.json").exists():
            return q
    raise FrozenError("frozen.json not found. Run from the project folder or set SHELL_LEDGER_HOME.")


def load_env(root: Path) -> None:
    f = root / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def frozen_sha256(root: Path) -> str:
    return hashlib.sha256((root / "frozen.json").read_bytes()).hexdigest()


def load_frozen(root: Path) -> dict:
    """Load the rules and refuse to run if they differ from the recorded hash."""
    digest = frozen_sha256(root)
    recorded = (root / "FROZEN.sha256").read_text().split()[0]
    if digest != recorded:
        raise FrozenError(
            "frozen.json does not match FROZEN.sha256. The rules were changed after the freeze; "
            "nothing will run until the original file is restored."
        )
    for f in sorted((root / "ledger" / "forecasts").glob("*.json")):
        if json.loads(f.read_text()).get("frozen_sha256") != digest:
            raise FrozenError(
                f"{f.name} was written under different rules. The rules cannot change once a forecast exists."
            )
    fz = json.loads((root / "frozen.json").read_text())
    fz["_sha256"] = digest
    return fz


def data_dir(root: Path) -> Path:
    d = root / "data"
    d.mkdir(exist_ok=True)
    return d


def ledger_dir(root: Path, *parts: str) -> Path:
    d = root.joinpath("ledger", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d
