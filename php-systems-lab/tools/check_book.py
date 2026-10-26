#!/usr/bin/env python3
"""Verify the book against its pinned GitHub sources.

Checks:
  * all expected chapter files exist (26 by default) and every fenced code
    block is closed;
  * every code block followed by a GitHub link matches the pinned lines;
  * every GitHub link points to an existing file and valid line range;
  * every local Markdown link in README.md resolves.

By default sources are fetched from raw.githubusercontent.com. For local work,
pass --source-root pointing at a directory with the project checkouts, e.g.
--source-root ../../php-systems-lab. That avoids the network entirely.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GITHUB = re.compile(
    r"https://github\.com/Researcher86/"
    r"(?P<repo>[^/]+)/blob/(?P<ref>[A-Za-z0-9._-]+)/"
    r"(?P<path>[^#\s)]+)"
    r"(?:#L(?P<start>\d+)(?:-L(?P<end>\d+))?)?"
)
FENCE = re.compile(r"^```")
LOCAL_MD = re.compile(r"\]\((?P<path>[^)#\s]+\.md)(?:#[^)]*)?\)")
PINNED_REF = re.compile(
    r"(?:[0-9a-f]{40}|v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
)


class SourceError(Exception):
    pass


def pinned(ref: str) -> bool:
    """A full 40-character SHA or a version tag, never a branch."""
    return bool(PINNED_REF.fullmatch(ref))


def dedent(lines: list[str]) -> list[str]:
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    shift = min(indents) if indents else 0
    return [line[shift:] for line in lines]


def read_local(root: Path, repo: str, path: str) -> list[str]:
    target = root / repo / path
    if not target.is_file():
        raise SourceError(f"file not found: {target}")
    return target.read_text(encoding="utf-8").splitlines()


def read_remote(repo: str, ref: str, path: str) -> list[str]:
    url = (
        "https://raw.githubusercontent.com/Researcher86/"
        f"{repo}/{ref}/{urllib.parse.quote(path, safe='/')}"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "engineering-lab-book-check/1.0"},
    )
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8").splitlines()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise SourceError(f"HTTP 404: {url}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        if attempt < 3:
            time.sleep(1 + attempt * 2)
    raise SourceError(f"could not fetch {url}: {last}")


class Sources:
    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.cache: dict[tuple[str, str, str], list[str]] = {}

    def lines(self, repo: str, ref: str, path: str) -> list[str]:
        key = (repo, ref, path)
        if key not in self.cache:
            if self.root is not None:
                self.cache[key] = read_local(self.root, repo, path)
            else:
                self.cache[key] = read_remote(repo, ref, path)
        return self.cache[key]


def blocks(text: str):
    """Yield (start, end, lang, body) for every fenced block."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not FENCE.match(lines[i]):
            i += 1
            continue
        lang = lines[i][3:].strip().split()[0] if lines[i][3:].strip() else ""
        j = i + 1
        while j < len(lines) and not FENCE.match(lines[j]):
            j += 1
        if j >= len(lines):
            raise ValueError(f"unclosed fence at line {i + 1}")
        yield i, j, lang, lines[i + 1 : j]
        i = j + 1


def next_content(lines: list[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=None)
    parser.add_argument(
        "--expected-chapters",
        type=int,
        default=26,
        help="number of numbered chapter files expected (default: 26)",
    )
    parser.add_argument("chapters", nargs="*", type=Path)
    args = parser.parse_args()

    if args.expected_chapters < 1:
        parser.error("--expected-chapters must be at least 1")

    chapters = args.chapters or sorted(Path(".").glob("[0-9][0-9]-*.md"))
    if not chapters:
        print("no chapters found", file=sys.stderr)
        return 2
    full_run = not args.chapters

    sources = Sources(args.source_root)
    failures: list[str] = []
    code_blocks = 0
    linked_blocks = 0
    exact_blocks = 0
    links = 0
    resolved = 0
    local_links = 0
    local_resolved = 0
    seen_links: set[str] = set()

    if full_run:
        expected = [
            f"{number:02d}" for number in range(1, args.expected_chapters + 1)
        ]
        actual = [chapter.name[:2] for chapter in chapters]
        if actual != expected:
            failures.append(
                f"expected chapters {expected[0]}..{expected[-1]}, got {actual}"
            )

    for chapter in chapters:
        text = chapter.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            chapter_blocks = list(blocks(text))
        except ValueError as exc:
            failures.append(f"{chapter}: {exc}")
            continue

        code_blocks += len(chapter_blocks)

        # Compare a code block with the source link immediately after it.
        for start, end, lang, body in chapter_blocks:
            look = next_content(lines, end + 1)
            if look >= len(lines):
                continue
            match = GITHUB.search(lines[look])
            if not match or not lines[look].lstrip().startswith("["):
                continue
            start_line = match["start"]
            if start_line is None:
                continue
            linked_blocks += 1
            repo, ref, path = match["repo"], match["ref"], match["path"]
            if not pinned(ref):
                failures.append(
                    f"{chapter}:{look + 1}: link ref is not a full SHA or "
                    f"version tag: {ref}"
                )
                continue
            first = int(start_line)
            last = int(match["end"] or first)
            if first < 1 or last < first:
                failures.append(
                    f"{chapter}:{look + 1}: inverted range L{first}-L{last}"
                )
                continue
            try:
                source = sources.lines(repo, ref, path)
            except SourceError as exc:
                failures.append(f"{chapter}:{look + 1}: {exc}")
                continue
            if last > len(source):
                failures.append(
                    f"{chapter}:{look + 1}: {path} has {len(source)} lines, "
                    f"link asks for L{first}-L{last}"
                )
                continue
            want = dedent(body)
            got = dedent(source[first - 1 : last])
            if want == got:
                exact_blocks += 1
                continue
            detail = ""
            for number in range(max(len(want), len(got))):
                left = want[number] if number < len(want) else "<missing>"
                right = got[number] if number < len(got) else "<missing>"
                if left != right:
                    detail = (
                        f"\n    first diff at block line {number + 1}:"
                        f"\n      book: {left!r}"
                        f"\n      source: {right!r}"
                    )
                    break
            failures.append(
                f"{chapter}:{look + 1}: block does not match "
                f"{repo}@{ref[:7]} {path} L{first}-L{last}{detail}"
            )

        # Check every GitHub link outside fenced code, not only block links.
        in_code = False
        for number, line in enumerate(lines, 1):
            if FENCE.match(line):
                in_code = not in_code
                continue
            if in_code:
                continue
            for match in GITHUB.finditer(line):
                links += 1
                key = match.group(0)
                seen_links.add(key)
                repo, ref, path = match["repo"], match["ref"], match["path"]
                if not pinned(ref):
                    failures.append(
                        f"{chapter}:{number}: link ref is not a full SHA or "
                        f"version tag: {ref}"
                    )
                    continue
                first = match["start"]
                last = match["end"] or first
                try:
                    source = sources.lines(repo, ref, path)
                except SourceError as exc:
                    failures.append(f"{chapter}:{number}: {exc}")
                    continue
                if first is None:
                    resolved += 1
                    continue
                start_line = int(first)
                end_line = int(last)
                if start_line < 1 or end_line < start_line:
                    failures.append(
                        f"{chapter}:{number}: inverted range "
                        f"L{start_line}-L{end_line}"
                    )
                    continue
                if end_line > len(source):
                    failures.append(
                        f"{chapter}:{number}: {path} has {len(source)} lines, "
                        f"link asks for L{start_line}-L{end_line}"
                    )
                    continue
                resolved += 1

    readme = Path("README.md")
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        try:
            list(blocks(text))
        except ValueError as exc:
            failures.append(f"{readme}: {exc}")
        targets: set[str] = set()
        in_code = False
        for number, line in enumerate(text.splitlines(), 1):
            if FENCE.match(line):
                in_code = not in_code
                continue
            if in_code:
                continue
            for match in LOCAL_MD.finditer(line):
                target = match["path"]
                if "://" in target:
                    continue
                local_links += 1
                targets.add(target)
                if (readme.parent / target).is_file():
                    local_resolved += 1
                else:
                    failures.append(
                        f"{readme}:{number}: local link does not resolve: {target}"
                    )
        if full_run:
            missing = {chapter.name for chapter in chapters} - targets
            if missing:
                failures.append(
                    f"{readme}: missing chapter links: {', '.join(sorted(missing))}"
                )

    print(
        f"chapters={len(chapters)} code_blocks={code_blocks} "
        f"linked={linked_blocks} exact={exact_blocks} "
        f"links={links} resolved={resolved} unique_links={len(seen_links)} "
        f"local_links={local_links} local_resolved={local_resolved}"
    )
    if failures:
        print(f"failures={len(failures)}")
        for failure in failures:
            print(f"  FAIL {failure}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
