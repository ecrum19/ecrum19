"""Update the profile README and releases page from the website feed.

The profile README is static content on GitHub, so this script fetches the
website's published recent-work feed, renders the 10 newest items into
README.md, derives released software entries for releases.md, and lets the
GitHub Action commit the refreshed files.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx


RECENT_WORK_COUNT = 10
RECENT_WORK_URL = (
    "https://raw.githubusercontent.com/ecrum19/eliascrum/master/public/recent_work.json"
)
README_MARKER = "contributions"
RECENT_RELEASES_MARKER = "recent_releases"
RELEASE_COUNT_MARKER = "release_count"

# Include the website repository itself even though it is not modeled as a
# software item in the current feed.
EXTRA_RELEASE_REPOS = [
    {
        "title": "Personal Website",
        "project_url": "https://eliascrum.github.io/eliascrum/",
        "source_url": "https://github.com/ecrum19/eliascrum",
        "description": "Source repository for the personal website.",
    }
]

ROOT = pathlib.Path(__file__).parent.resolve()
README_PATH = ROOT / "README.md"
RELEASES_PATH = ROOT / "releases.md"


class GitHubReleaseResolver:
    """Resolve release metadata for GitHub repositories with lightweight caching."""

    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ecrum19-profile-readme-updater",
            },
        )
        self.cache: dict[str, dict[str, Any] | None] = {}

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self.client.close()

    def resolve(self, repo_url: str) -> dict[str, Any] | None:
        """Return the latest release, or latest tag when no release exists."""
        repo = parse_github_repo(repo_url)
        if not repo:
            return None

        if repo in self.cache:
            return self.cache[repo]

        resolved = self._fetch_latest_release(repo)
        if resolved is None:
            resolved = self._fetch_latest_tag(repo)

        self.cache[repo] = resolved
        return resolved

    def _fetch_latest_release(self, repo: str) -> dict[str, Any] | None:
        response = self.client.get(f"https://api.github.com/repos/{repo}/releases/latest")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        payload = response.json()
        release_name = first_non_empty(payload, "name", "tag_name")
        release_date = first_non_empty(payload, "published_at", "created_at")
        release_url = first_non_empty(payload, "html_url")
        if not release_name or not release_date or not release_url:
            return None

        return {
            "release_name": str(release_name).strip(),
            "release_date": parse_date(release_date),
            "release_url": str(release_url).strip(),
            "source": "github-release",
        }

    def _fetch_latest_tag(self, repo: str) -> dict[str, Any] | None:
        response = self.client.get(f"https://api.github.com/repos/{repo}/tags")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        tags = response.json()
        if not tags:
            return None

        latest_tag = tags[0]
        tag_name = latest_tag.get("name")
        commit_api_url = latest_tag.get("commit", {}).get("url")
        if not tag_name or not commit_api_url:
            return None

        commit_response = self.client.get(commit_api_url)
        commit_response.raise_for_status()
        commit_payload = commit_response.json()
        commit_date = first_non_empty(
            commit_payload.get("commit", {}).get("committer", {}),
            "date",
        )
        if not commit_date:
            return None

        return {
            "release_name": str(tag_name).strip(),
            "release_date": parse_date(commit_date),
            "release_url": f"https://github.com/{repo}/tree/{tag_name}",
            "source": "github-tag",
        }


def fetch_recent_work_payload() -> dict[str, Any]:
    """Fetch the website feed once so both README outputs use the same data."""
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(RECENT_WORK_URL)

    response.raise_for_status()

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        preview = response.text[:200].replace("\n", " ")
        raise RuntimeError(
            "recent_work.json did not return valid JSON. "
            f"Received content starting with: {preview!r}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("recent_work.json must contain a top-level JSON object.")
    return payload


def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Accept the current feed shape and a few close variants."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("recentWork", "items", "recent_work", "work", "works", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def fetch_recent_work_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the feed into the newest recent-work entries for README.md."""
    items = extract_items(payload)
    if not items:
        raise RuntimeError("recent_work.json returned JSON, but no work items were found.")

    normalized = [normalize_recent_work_item(item, index) for index, item in enumerate(items)]
    normalized.sort(key=recent_work_sort_key, reverse=True)
    return normalized[:RECENT_WORK_COUNT]


def normalize_recent_work_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    """Map a feed item into the fields needed for README rendering."""
    title = first_non_empty(item, "title", "name", "label")
    url = first_non_empty(item, "itemUrl", "url", "link", "href")
    description = first_non_empty(item, "summary", "description", "excerpt", "details")
    date_value = first_non_empty(
        item,
        "dateIso",
        "date",
        "published_at",
        "publishedAt",
        "updated_at",
        "updatedAt",
        "created_at",
        "createdAt",
    )

    if not title:
        raise RuntimeError(f"Feed item is missing a title-like field: {item!r}")
    if not url:
        raise RuntimeError(f"Feed item is missing a URL-like field: {item!r}")

    return {
        "title": str(title).strip(),
        "url": str(url).strip(),
        "description": collapse_whitespace(str(description).strip()) if description else "",
        "date": parse_date(date_value),
        "order": index,
    }


def build_release_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build unique software release entries for releases.md."""
    items = extract_items(payload)
    resolver = GitHubReleaseResolver()
    try:
        release_entries: list[dict[str, Any]] = []

        for item in items:
            if item.get("type") != "Software":
                continue

            release_entry = normalize_release_item(item, resolver)
            if release_entry is not None:
                release_entries.append(release_entry)

        for extra in EXTRA_RELEASE_REPOS:
            release_entry = normalize_release_item(
                {
                    "type": "Software",
                    "title": extra["title"],
                    "itemUrl": extra["project_url"],
                    "url": extra["project_url"],
                    "summary": extra["description"],
                    "sourceUrl": extra["source_url"],
                },
                resolver,
            )
            if release_entry is not None:
                release_entries.append(release_entry)
    finally:
        resolver.close()

    deduped: dict[str, dict[str, Any]] = {}
    for entry in release_entries:
        key = release_identity(entry)
        existing = deduped.get(key)
        if existing is None or release_sort_key(entry) > release_sort_key(existing):
            deduped[key] = entry

    ordered = sorted(deduped.values(), key=release_sort_key, reverse=True)
    return ordered


def normalize_release_item(
    item: dict[str, Any],
    resolver: GitHubReleaseResolver,
) -> dict[str, Any] | None:
    """Create a release entry from a software item plus GitHub metadata fallback."""
    title = first_non_empty(item, "title", "name", "label")
    project_url = first_non_empty(item, "sourceUrl", "itemUrl", "url", "link", "href")
    page_url = first_non_empty(item, "itemUrl", "url", "link", "href")
    description = first_non_empty(item, "summary", "description", "excerpt", "details")
    latest_release = first_non_empty(item, "latestRelease")
    latest_release_date = first_non_empty(item, "latestReleaseDateIso")

    if not title or not project_url:
        return None

    github_release = resolver.resolve(str(project_url).strip())
    release_name = str(latest_release).strip() if latest_release else None
    release_date = parse_date(latest_release_date) if latest_release_date else None
    release_url = None

    if github_release is not None:
        release_url = github_release["release_url"]
        if release_name is None:
            release_name = github_release["release_name"]
        if release_date is None or release_date == datetime.min.replace(tzinfo=UTC):
            release_date = github_release["release_date"]

    if release_name is None or release_date is None:
        return None

    if release_date == datetime.min.replace(tzinfo=UTC):
        return None

    return {
        "title": str(title).strip(),
        "project_url": str(project_url).strip(),
        "page_url": str(page_url).strip() if page_url else str(project_url).strip(),
        "description": collapse_whitespace(str(description).strip()) if description else "",
        "release_name": release_name,
        "release_url": release_url or str(project_url).strip(),
        "release_date": release_date,
        "release_date_iso": release_date.date().isoformat(),
    }


def release_identity(item: dict[str, Any]) -> str:
    """Use the repo URL when available so the same project is listed once."""
    repo = parse_github_repo(item["project_url"])
    if repo:
        return f"github:{repo.lower()}"
    return f"url:{item['page_url'].lower()}"


def parse_github_repo(url: str | None) -> str | None:
    """Extract owner/repo from a GitHub repository URL."""
    if not url:
        return None

    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def first_non_empty(item: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value for the given keys."""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def collapse_whitespace(value: str) -> str:
    """Keep README lines compact even if the source text contains newlines."""
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value: Any) -> datetime:
    """Parse common timestamp formats; unknown dates sort to the bottom."""
    if value is None:
        return datetime.min.replace(tzinfo=UTC)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return datetime.min.replace(tzinfo=UTC)

        for candidate in (text, text.replace("Z", "+00:00")):
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass

        try:
            parsed = parsedate_to_datetime(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)

    return datetime.min.replace(tzinfo=UTC)


def recent_work_sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
    """Sort by recency while preserving the feed order for same-day items."""
    return item["date"], -item["order"]


def release_sort_key(item: dict[str, Any]) -> tuple[datetime, str]:
    """Sort releases newest-first, with title as a stable tiebreaker."""
    return item["release_date"], item["title"].lower()


def render_recent_work_markdown(items: list[dict[str, Any]]) -> str:
    """Convert normalized work items into the README bullet list."""
    lines = []
    for item in items:
        line = f"* [{item['title']}]({item['url']})"
        if item["description"]:
            line += f" - {item['description']}"
        lines.append(line)
    return "\n".join(lines)


def render_releases_markdown(items: list[dict[str, Any]]) -> str:
    """Convert release entries into the releases.md list format."""
    lines = []
    for item in items:
        lines.append(
            (
                "* **[{title}]({project_url})**: "
                "[{release_name}]({release_url}) - {release_date_iso}\n"
                "<br>{description}"
            ).format(**item)
        )
    return "\n\n".join(lines)


def replace_chunk(content: str, marker: str, chunk: str, *, inline: bool = False) -> str:
    """Replace the content between two marker comments."""
    pattern = re.compile(
        rf"<!\-\- {re.escape(marker)} starts \-\->.*?<!\-\- {re.escape(marker)} ends \-\->",
        re.DOTALL,
    )
    if inline:
        replacement = f"<!-- {marker} starts -->{chunk}<!-- {marker} ends -->"
    else:
        replacement = f"<!-- {marker} starts -->\n{chunk}\n<!-- {marker} ends -->"
    return pattern.sub(replacement, content)


def main() -> None:
    """Update README.md and releases.md from the same feed snapshot."""
    payload = fetch_recent_work_payload()

    readme_contents = README_PATH.read_text(encoding="utf-8")
    recent_work = fetch_recent_work_items(payload)
    readme_markdown = render_recent_work_markdown(recent_work)
    rewritten_readme = replace_chunk(readme_contents, README_MARKER, readme_markdown)
    README_PATH.write_text(rewritten_readme, encoding="utf-8")

    releases_contents = RELEASES_PATH.read_text(encoding="utf-8")
    release_entries = build_release_entries(payload)
    releases_markdown = render_releases_markdown(release_entries)
    rewritten_releases = replace_chunk(
        releases_contents,
        RECENT_RELEASES_MARKER,
        releases_markdown,
    )
    rewritten_releases = replace_chunk(
        rewritten_releases,
        RELEASE_COUNT_MARKER,
        str(len(release_entries)),
        inline=True,
    )
    RELEASES_PATH.write_text(rewritten_releases, encoding="utf-8")


if __name__ == "__main__":
    main()
