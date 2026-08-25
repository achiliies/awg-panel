"""Check the panel's dependency pins. Driven by tests/deps.sh, not run alone.

Five properties, each one a way the pinning has actually been observed to rot
in projects that set it up and walked away:

  1. Every line in every lock is pinned with == and carries at least one
     sha256. One entry losing its hashes is enough to put pip back to trusting
     whatever the index hands over, and --require-hashes refuses the whole file
     rather than that one line, so the failure lands on a server mid-install.
  2. Every range in requirements.txt is satisfied by requirements.lock. A range
     edited without regenerating leaves the two describing different software.
  3. requirements.txt and pyproject.toml agree. They are two copies of one
     list, which is a comment's worth of discipline until something enforces it.
  4. requirements-dev.lock contains requirements.lock exactly, version for
     version. CI installs the development lock; if it resolved its own Django,
     the suite would be proving something about software no server runs.
  5. Every development range in pyproject.toml is satisfied by that lock, so
     ruff and pytest are pinned like everything else - a formatter that moves
     on its own turns `ruff format --check` into a gate that fails for reasons
     nobody chose.
"""

import re
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # python 3.10, which install-panel.sh still accepts
    import tomli as tomllib

# A locked line: the name and version at column zero, then an optional
# environment marker that universal resolution attaches for the platforms and
# interpreters this one is not.
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")

failures: list[str] = []


def fail(where: str, message: str) -> None:
    failures.append(f"{where}: {message}")


def read_lock(path: Path) -> dict[str, tuple[Version, int]]:
    """Every pin in a lock file, as name -> (version, how many hashes)."""
    found: dict[str, tuple[Version, int]] = {}
    name = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not raw[:1].isspace():
            match = PIN.match(line)
            if not match:
                # Anything at column zero that is not a pin is either a bare
                # requirement or a stray -e / -r, and both mean an unpinned
                # install once --require-hashes is not looking.
                fail(path.name, f"not pinned with ==: {line.rstrip(' \\')}")
                name = ""
                continue
            name = canonicalize_name(match.group(1))
            found[name] = (Version(match.group(2)), 0)
        elif line.startswith("--hash=sha256:") and name:
            version, count = found[name]
            found[name] = (version, count + 1)
    for pinned, (version, hashes) in found.items():
        if hashes == 0:
            fail(path.name, f"{pinned}=={version} carries no --hash")
    return found


def read_ranges(lines: list[str]) -> dict[str, Requirement]:
    out = {}
    for line in lines:
        text = line.split("#", 1)[0].strip()
        if text:
            requirement = Requirement(text)
            out[canonicalize_name(requirement.name)] = requirement
    return out


def check_satisfied(ranges: dict[str, Requirement], lock: dict, where: str) -> None:
    for name, requirement in ranges.items():
        if name not in lock:
            fail(where, f"{requirement} is declared but missing from the lock")
        elif not requirement.specifier.contains(lock[name][0], prereleases=True):
            fail(where, f"{lock[name][0]} does not satisfy {requirement}")


panel = Path(sys.argv[1])
pyproject = tomllib.loads((panel / "pyproject.toml").read_text(encoding="utf-8"))

runtime = read_lock(panel / "requirements.lock")
develop = read_lock(panel / "requirements-dev.lock")
read_lock(panel / "requirements-bootstrap.lock")

declared = read_ranges((panel / "requirements.txt").read_text(encoding="utf-8").splitlines())
check_satisfied(declared, runtime, "requirements.txt")

project = read_ranges(pyproject["project"]["dependencies"])
if project != declared:
    only_here = sorted(str(r) for n, r in declared.items() if project.get(n) != r)
    only_there = sorted(str(r) for n, r in project.items() if declared.get(n) != r)
    fail(
        "pyproject.toml",
        "the dependency list has drifted from requirements.txt; they are one list "
        f"kept in two files.\n    requirements.txt: {only_here or 'nothing unique'}"
        f"\n    pyproject.toml:   {only_there or 'nothing unique'}",
    )

for name, (version, _) in runtime.items():
    if name not in develop:
        fail("requirements-dev.lock", f"{name} is in the runtime lock but not here")
    elif develop[name][0] != version:
        fail(
            "requirements-dev.lock",
            f"{name}=={develop[name][0]} but servers get {version}; the development "
            "lock must be resolved against the runtime one",
        )

check_satisfied(
    read_ranges(pyproject["project"]["optional-dependencies"]["dev"]),
    develop,
    "pyproject.toml [dev]",
)

if failures:
    print(f"dependency pins: {len(failures)} problem(s)\n")
    for problem in failures:
        print(f"  {problem}")
    print("\nRegenerate with: ./lock-deps.sh   (in panel/, then commit both locks)")
    sys.exit(1)

print(f"dependency pins: {len(runtime)} runtime, {len(develop)} development, all pinned and hashed")
