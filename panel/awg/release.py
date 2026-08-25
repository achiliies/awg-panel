"""Which published release is the newest stable one, and is it newer than this.

The one place that answers those two questions. Both front ends ask it: the
panel imports :func:`check`, and ``bin/awg-update`` runs this file as a script
for the same answer in JSON. That is the whole reason it lives in ``awg/``,
which is the Django-free half of the panel - a second implementation in bash
would be a second set of rules for what counts as newer, and the two would
disagree on the day it mattered.

Nothing here is allowed to fail on the network. A server that cannot reach
GitHub is the normal case behind a strict egress policy, and the answer to
"is there an update" on one of those is "could not tell", not an error.
"""

import json
import os
import re
import urllib.error
import urllib.request

# The repository releases are published from. Overridable, because this tree is
# published under whatever name its operator forked it to and pointing the check
# at a repository that is not the one installed would offer false upgrades - but
# no longer *required*, which it used to be. Requiring it meant every stock
# install had the check switched off until somebody edited a file they had never
# been told about, so the feature existed and nobody had it.
DEFAULT_REPO = "achiliies/awg-panel"

ENV_REPO = "AWG_PANEL_UPDATE_REPO"
ENV_URL = "AWG_PANEL_UPDATE_URL"

RELEASE_URL = "https://api.github.com/repos/{repo}/releases/latest"

# The asset an update installs: the bundle carrying the panel, its prebuilt UI
# and every Python wheel.
ASSET_BUNDLE = "awg-panel.sh"
# Published beside it by .github/workflows/release.yml. An update refuses to run
# a bundle this file does not vouch for.
ASSET_SUMS = "SHA256SUMS"

TIMEOUT = 8.0
# A release body is shown, not parsed; a few kilobytes is a change log, anything
# past it is somebody's essay.
NOTES_LIMIT = 2000
# Bounded read: this is a remote body and nothing needs more than the first few
# kilobytes of it.
BODY_LIMIT = 262144

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# Semantic versioning's own shape, with the leading "v" release tags carry and
# with the patch part optional, because "v1.2" is a tag people cut.
_SEMVER_RE = re.compile(
    r"^v?(?P<nums>\d+(?:\.\d+)*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_NUMERIC_ID = re.compile(r"^\d+$")

# How many numeric parts every version is padded to, so "1.2" and "1.2.0"
# compare equal rather than one being short.
_PARTS = 4


class Unparseable(ValueError):
    """The text is not a version this can order against another one."""


def parse_version(text: str) -> tuple:
    """A sort key for a release tag, ordered the way semver says.

    Two things matter and neither is obvious from a naive split. A pre-release
    sorts *below* the release it is a candidate for - 1.0.0-rc1 comes before
    1.0.0 - and inside a pre-release a numeric identifier sorts below an
    alphanumeric one and numerically against another number, so rc.9 comes
    before rc.10 rather than after it.

    The old rule here was "pull every run of digits out and compare those",
    which read 1.0.0-rc1 as (1, 0, 0, 1) and so as *newer* than 1.0.0. Every
    server would have been offered an upgrade from the release to the candidate
    it was cut from.
    """
    match = _SEMVER_RE.match((text or "").strip())
    if not match:
        raise Unparseable(f"{text!r} is not a version number")
    nums = [int(part) for part in match.group("nums").split(".")[:_PARTS]]
    nums += [0] * (_PARTS - len(nums))
    pre = match.group("pre")
    if not pre:
        # 1 rather than 0, so a release outranks every candidate for it. The
        # empty tuple after it is never reached in a comparison against a
        # pre-release, and keeps the two keys the same shape.
        return (tuple(nums), 1, ())
    ids = []
    for part in pre.split("."):
        if _NUMERIC_ID.match(part):
            ids.append((0, int(part), ""))
        else:
            ids.append((1, 0, part))
    return (tuple(nums), 0, tuple(ids))


def is_newer(latest: str, current: str) -> bool:
    """Is `latest` a version somebody on `current` should be offered?

    False on anything unreadable, in both directions. A tag nobody can order is
    not a reason to tell an operator their server is out of date, and an
    installed version nobody can order is not a reason to offer them every
    release there has ever been.
    """
    try:
        return parse_version(latest) > parse_version(current)
    except Unparseable:
        return False


def is_prerelease_tag(tag: str) -> bool:
    """Does the tag itself say it is a candidate, whatever the feed claims?

    The feed is asked first and this is the belt: `/releases/latest` already
    excludes pre-releases, and a hand-set AWG_PANEL_UPDATE_URL can point
    anywhere. A hyphen in the tag is semver's pre-release marker, and it is what
    .github/workflows/release.yml decides the `prerelease` flag from - so the
    two agree by construction and this only has to catch a feed that is not
    ours.
    """
    try:
        return parse_version(tag)[1] == 0
    except Unparseable:
        return False


def release_url() -> str:
    """The feed to read, or "" when nothing points at one.

    A full URL wins over a repository name so an operator behind a mirror can
    say exactly what to fetch. Both come from the environment, which for the
    panel and the collector is /etc/awg-panel.env.
    """
    direct = (os.environ.get(ENV_URL) or "").strip()
    if direct.startswith("https://"):
        return direct
    repo = (os.environ.get(ENV_REPO) or "").strip() or DEFAULT_REPO
    if _REPO_RE.match(repo):
        return RELEASE_URL.format(repo=repo)
    return ""


def fetch_release(url: str, *, version: str = "", timeout: float = TIMEOUT) -> dict:
    """The newest stable release the feed offers, as GitHub describes it.

    A feed answering with a list is read as /releases rather than
    /releases/latest and the newest publishable entry is picked out of it, so a
    URL pointing at either shape works. Drafts and pre-releases are dropped
    here, which is the only place a list can be filtered at all.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"awg-panel/{version or 'unknown'}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(BODY_LIMIT).decode("utf-8", "replace"))
    if isinstance(payload, list):
        stable = [
            entry
            for entry in payload
            if isinstance(entry, dict)
            and not entry.get("draft")
            and not entry.get("prerelease")
            and not is_prerelease_tag(str(entry.get("tag_name") or ""))
        ]
        if not stable:
            raise ValueError("the release feed lists no stable release")
        payload = max(stable, key=lambda entry: _sort_key(str(entry.get("tag_name") or "")))
    if not isinstance(payload, dict):
        raise ValueError("the release feed did not answer with a release")
    return payload


def _sort_key(tag: str) -> tuple:
    """parse_version, but ordering the unreadable below everything readable."""
    try:
        return (1, parse_version(tag))
    except Unparseable:
        return (0, ((0,) * _PARTS, 0, ()))


def asset_urls(release: dict) -> dict[str, str]:
    """Download URL per asset name, for the two an update needs."""
    urls = {}
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        href = str(asset.get("browser_download_url") or "")
        if name and href:
            urls[name] = href
    return urls


def blank(current: str) -> dict:
    """The answer's shape with nothing filled in.

    Always the same keys, checked or not, so neither front end has an optional
    branch to get wrong.
    """
    return {
        "checked": False,
        "reason": "",
        "current": current,
        "latest": None,
        "update_available": False,
        "url": "",
        "notes": "",
        "published_at": None,
        "asset": "",
        "sums": "",
    }


def check(current: str, *, timeout: float = TIMEOUT) -> dict:
    """Compare the installed version against the newest stable release.

    Never raises. Every way this can fail to reach an answer comes back as
    ``checked: False`` with a sentence saying which one it was, because a panel
    that cannot reach GitHub is not a panel that is broken and must not look
    like one.
    """
    answer = blank(current)

    url = release_url()
    if not url:
        answer["reason"] = (
            "No release source is configured, so there is nothing to compare against. "
            f"Set {ENV_REPO}=<owner>/<repo> in /etc/awg-panel.env."
        )
        return answer

    try:
        release = fetch_release(url, version=current, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Told apart from the rest because the two that happen are worth
        # different sentences: a repository with no release yet answers 404,
        # and an IP that has checked too often answers 403.
        if exc.code == 404:
            answer["reason"] = (
                "The repository has published no release yet, so there is nothing to "
                "compare against."
            )
        elif exc.code in (403, 429):
            answer["reason"] = (
                "GitHub is rate-limiting this server's checks. Nothing is wrong; try "
                "again in an hour."
            )
        else:
            answer["reason"] = f"The release feed answered {exc.code}. Try again later."
        return answer
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        answer["reason"] = f"The release list could not be reached ({exc}). Try again later."
        return answer

    return _describe(answer, release)


def _describe(answer: dict, release: dict) -> dict:
    """Fill in a blank answer from one release, and decide whether to offer it.

    Split out of check() so a mirrored feed reaches exactly the same rules: the
    pre-release refusal, the version comparison and the asset lookup are what
    an update turns on, and a second path through them would be a second set of
    answers.
    """
    if not isinstance(release, dict):
        answer["reason"] = "The release description is not a release."
        return answer

    latest = str(release.get("tag_name") or "").strip()
    answer["checked"] = True
    answer["latest"] = latest or None
    answer["url"] = str(release.get("html_url") or "")
    answer["notes"] = str(release.get("body") or "")[:NOTES_LIMIT]
    answer["published_at"] = release.get("published_at") or None

    if not latest:
        answer["reason"] = "The newest release has no version tag, so it cannot be compared."
        return answer
    if release.get("draft") or release.get("prerelease") or is_prerelease_tag(latest):
        answer["reason"] = (
            f"The newest release ({latest}) is a pre-release. Updates only follow stable releases."
        )
        return answer
    try:
        parse_version(latest)
    except Unparseable:
        answer["reason"] = (
            f"The newest release is tagged {latest!r}, which is not a version this can "
            "compare against."
        )
        return answer

    answer["update_available"] = is_newer(latest, answer["current"])

    # Resolved whatever the comparison decided, and that is the point. These
    # two fields used to be filled in only when an upgrade was on offer, which
    # left them empty on the one run that most needs them: `awg-update apply
    # --force` reinstalls the release already on the server - the documented
    # repair for an install that stopped half way, and the only route back for
    # a server whose VERSION is already the new one - and it resolves the
    # bundle out of this same answer. It therefore failed every time, with
    # "release X carries no awg-panel.sh", which blamed the release for it.
    urls = asset_urls(release)
    answer["asset"] = urls.get(ASSET_BUNDLE, "")
    answer["sums"] = urls.get(ASSET_SUMS, "")
    if answer["update_available"] and not answer["asset"]:
        # Reachable when a release was cut by hand rather than by the workflow.
        # Said plainly rather than left for the download to fail on: there is
        # nothing here for an update to install. Only said when an upgrade is
        # what was being offered - a server already on the newest release is
        # not out of date because that release carries no installer.
        answer["reason"] = (
            f"Release {latest} carries no {ASSET_BUNDLE}, so it cannot be installed "
            "from here. Update from a checkout instead."
        )
    return answer


def check_feed(current: str, path: str) -> dict:
    """The same answer, from a release description already on disk.

    For a server that cannot reach GitHub at all and mirrors releases itself:
    drop the release JSON somewhere, point AWG_UPDATE_FEED at it, and the whole
    of the rest of the update works unchanged - including, since the asset URLs
    in it may be file:// ones, the download. It is also how tests/update.sh
    drives this without a network.

    Everything after the fetch is the code path a real check takes. That is the
    point of doing it here rather than in the caller: a seam that skipped the
    version comparison or the pre-release rule would be a seam that tested
    nothing worth testing.
    """
    answer = blank(current)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        answer["reason"] = f"The release description at {path} could not be read ({exc})."
        return answer
    return _describe(answer, payload)


def _main(argv: list[str]) -> int:
    """`python3 release.py [--feed FILE] <installed-version>` - the answer as JSON.

    What bin/awg-update runs. Prints on one line and exits 0 whatever the
    answer, including "could not check": the caller reads the object, and an
    exit status would only be a second, coarser copy of what is in it.
    """
    args = argv[1:]
    feed = ""
    if args and args[0] == "--feed":
        feed = args[1] if len(args) > 1 else ""
        args = args[2:]
    current = args[0] if args else "0.0.0"
    print(json.dumps(check_feed(current, feed) if feed else check(current)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by tests/update.sh
    import sys

    raise SystemExit(_main(sys.argv))
