"""
Safe, format-preserving editor for the registry `scripts/registry *.json`
files — used by the update-availability scanner's "Dismiss" (adds an
`available: false` reference row) and "Include" (adds a real, enabled
bucket) actions.

Why not just json.load() + json.dump()? These files are carefully,
manually hand-formatted (confirmed: a real json.dumps(data, indent=2)
round-trip on registry python.json does NOT reproduce the original text
byte-for-byte) — most languages write every version bucket on a single
line (`{ "nr": "9", "release_date": "...", "compatibility": [ ... ] }`),
while Python's own registry uses full multi-line indentation instead. A
blind re-dump would reformat every entry in the file, turning a one-line
addition into a multi-thousand-line diff and destroying the manually
curated style this project has relied on (via the Edit tool, by hand) for
this entire session. Instead this does surgical text insertion: find the
exact byte span of the target entry's "version" array (bracket-depth
aware, so nested compatibility arrays don't confuse it), detect whether
ITS entries are single-line or multi-line style from the first one, and
insert a matching new entry right before the array's closing bracket.

Every write is validated (re-parsed with json.loads) before being
committed to disk — if the surgery ever produces invalid JSON, nothing is
written and a RegistryWriteError is raised instead.
"""

import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


class RegistryWriteError(Exception):
    pass


def _find_bracket_span(text: str, open_pos: int) -> int:
    """Given the position of an opening '[' or '{', return the position of
    its matching closing bracket (bracket-depth aware over both [] and {})."""
    opener = text[open_pos]
    depth = 1
    i = open_pos + 1
    n = len(text)
    while depth > 0:
        if i >= n:
            raise RegistryWriteError("unbalanced brackets while scanning registry text")
        c = text[i]
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return i - 1


def _section_span(text: str, section_key: str) -> tuple:
    """Byte span (start, end) of the top-level 'frameworks' or
    'cryptography_libs' array's contents, so a name search never leaks into
    the other section."""
    key_pos = text.index(f'"{section_key}"')
    open_pos = text.index("[", key_pos)
    close_pos = _find_bracket_span(text, open_pos)
    return open_pos, close_pos


def _find_version_array(text: str, section_key: str, name: str) -> tuple:
    """Byte span (open_bracket_pos, close_bracket_pos) of the target
    framework/library's own "version": [ ... ] array."""
    sec_start, sec_end = _section_span(text, section_key)
    name_pat = f'"name": "{name}"'
    try:
        name_pos = text.index(name_pat, sec_start, sec_end)
    except ValueError:
        raise RegistryWriteError(
            f'entry "{name}" not found in section "{section_key}"')
    version_pat = '"version": ['
    version_pos = text.index(version_pat, name_pos, sec_end)
    open_pos = version_pos + len(version_pat) - 1
    close_pos = _find_bracket_span(text, open_pos)
    return open_pos, close_pos


def _fmt(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _format_bucket_singleline(nr, release_date, compatibility, available) -> str:
    avail = f'"available": {_fmt(available)}, ' if available is not None else ""
    compat = ("[ " + ", ".join(_fmt(c) for c in compatibility) + " ]") if compatibility else "[]"
    return (f'{{ "nr": {_fmt(nr)}, "release_date": {_fmt(release_date)}, '
            f'{avail}"compatibility": {compat} }}')


def _format_bucket_multiline(nr, release_date, compatibility, available, indent: int) -> str:
    pad, pad2, pad3 = " " * indent, " " * (indent + 2), " " * (indent + 4)
    lines = [pad + "{", pad2 + f'"nr": {_fmt(nr)},', pad2 + f'"release_date": {_fmt(release_date)},']
    if available is not None:
        lines.append(pad2 + f'"available": {_fmt(available)},')
    if compatibility:
        lines.append(pad2 + '"compatibility": [')
        for i, c in enumerate(compatibility):
            comma = "," if i < len(compatibility) - 1 else ""
            lines.append(pad3 + f"{_fmt(c)}{comma}")
        lines.append(pad2 + "]")
    else:
        lines.append(pad2 + '"compatibility": []')
    lines.append(pad + "}")
    return "\n".join(lines)


def add_bucket(registry_path: Path, section_key: str, name: str,
              nr: str, release_date, compatibility: list,
              available: bool | None = None) -> None:
    """Insert a new version bucket for framework/library `name` (in
    `section_key`, "frameworks" or "cryptography_libs") right after its
    current last bucket. `available=False` marks it a dismissed/reference
    row (matching this project's existing convention); `available=None`
    (the default) adds a normal, fully-tracked bucket.

    Raises RegistryWriteError if the entry can't be found, `nr` is ALREADY
    a bucket for this framework/library (e.g. a stale pending-update row
    from a database that's out of sync with this registry file -- two
    independent SQLite databases, one per Docker host, can each think the
    same major is still "pending" after one of them has already acted on
    it), or the result would not be valid JSON. Nothing is written to disk
    in any of those cases.
    """
    text = registry_path.read_text(encoding="utf-8")
    open_pos, close_pos = _find_version_array(text, section_key, name)

    existing_nrs = {m.group(1) for m in re.finditer(r'"nr":\s*"([^"]+)"', text[open_pos:close_pos])}
    if nr in existing_nrs:
        raise RegistryWriteError(
            f'"{name}" already has a bucket for nr={nr!r} -- refusing to add a duplicate')

    first_brace = text.index("{", open_pos)
    line_start = text.rfind("\n", 0, first_brace) + 1
    indent_str = text[line_start:first_brace]
    indent = len(indent_str)

    first_entry_end = _find_bracket_span(text, first_brace) + 1
    multiline = "\n" in text[first_brace:first_entry_end]

    if multiline:
        new_entry = _format_bucket_multiline(nr, release_date, compatibility, available, indent)
    else:
        new_entry = indent_str + _format_bucket_singleline(nr, release_date, compatibility, available)

    last_brace_end = text.rfind("}", open_pos, close_pos) + 1
    j = last_brace_end
    while text[j] in " \t\r\n":
        j += 1
    needs_comma = text[j] != ","

    prefix = text[:last_brace_end]
    suffix = text[last_brace_end:]
    comma = "," if needs_comma else ""
    new_text = f"{prefix}{comma}\n{new_entry}{suffix}"

    try:
        json.loads(new_text)
    except json.JSONDecodeError as exc:
        raise RegistryWriteError(
            f"surgical insert for {name!r} nr={nr!r} produced invalid JSON: {exc}"
        ) from exc

    registry_path.write_text(new_text, encoding="utf-8")


def update_bucket_compatibility(registry_path: Path, section_key: str, name: str,
                                nr: str, compatibility: list) -> None:
    """Replace an EXISTING bucket's compatibility array in place -- for
    flipping a version from available:false (a still-prerelease placeholder,
    compatibility left empty) to available:true once it actually ships.
    Flipping `available` alone is NOT enough for these: an empty
    compatibility array means generate_images.py has no language version to
    build it against, so it silently produces zero images even though the
    bucket now reads as "included" (found the hard way with Tink 1.23).

    Raises RegistryWriteError if the entry/bucket isn't found or the result
    wouldn't be valid JSON. Nothing is written to disk in that case."""
    text = registry_path.read_text(encoding="utf-8")
    open_pos, close_pos = _find_version_array(text, section_key, name)

    nr_pat = f'"nr": {_fmt(nr)}'
    try:
        nr_pos = text.index(nr_pat, open_pos, close_pos)
    except ValueError:
        raise RegistryWriteError(
            f'"{name}" has no existing bucket for nr={nr!r} to update')

    # The bucket's own enclosing { ... }, bracket-depth aware.
    bucket_open = text.rindex("{", open_pos, nr_pos)
    bucket_close = _find_bracket_span(text, bucket_open)
    bucket_text = text[bucket_open:bucket_close + 1]

    compat_key_pos = bucket_text.index('"compatibility"')
    array_open = bucket_text.index("[", compat_key_pos)
    array_close = _find_bracket_span(bucket_text, array_open)
    multiline = "\n" in bucket_text[array_open:array_close]

    if multiline:
        line_start = text.rfind("\n", 0, bucket_open) + 1
        base_indent = text[line_start:bucket_open]
        pad2, pad3 = base_indent + "  ", base_indent + "    "
        if compatibility:
            inner = "[\n" + "".join(
                f"{pad3}{_fmt(c)}{',' if i < len(compatibility) - 1 else ''}\n"
                for i, c in enumerate(compatibility)
            ) + pad2 + "]"
        else:
            inner = "[]"
    else:
        inner = ("[ " + ", ".join(_fmt(c) for c in compatibility) + " ]") if compatibility else "[]"

    new_bucket_text = bucket_text[:compat_key_pos] + f'"compatibility": {inner}' + bucket_text[array_close + 1:]
    new_text = text[:bucket_open] + new_bucket_text + text[bucket_close + 1:]

    try:
        json.loads(new_text)
    except json.JSONDecodeError as exc:
        raise RegistryWriteError(
            f"compatibility update for {name!r} nr={nr!r} produced invalid JSON: {exc}"
        ) from exc

    registry_path.write_text(new_text, encoding="utf-8")


def registry_path_for(language: str) -> Path:
    return SCRIPT_DIR / f"registry {language}.json"


def bucket_exists(registry_path: Path, section_key: str, name: str, nr: str) -> bool:
    """True if `name` already has a bucket for `nr` -- e.g. a stale pending-
    update row from a DIFFERENT host's database (each Docker host has its
    own SQLite db, but they all edit the same checked-in registry file) that
    thinks a major is still pending after another host already acted on it.
    Callers should treat this as "already done", not an error."""
    text = registry_path.read_text(encoding="utf-8")
    try:
        open_pos, close_pos = _find_version_array(text, section_key, name)
    except RegistryWriteError:
        return False
    existing_nrs = {m.group(1) for m in re.finditer(r'"nr":\s*"([^"]+)"', text[open_pos:close_pos])}
    return nr in existing_nrs


def get_entry_compatibility(registry_path: Path, section_key: str, name: str, nr: str) -> list | None:
    """Read (not write) the compatibility array of an already-tracked
    bucket, e.g. to inherit it for a newly-included sibling version."""
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    for lang in data["languages"]:
        for entry in lang.get(section_key, []):
            if entry.get("name") == name:
                for v in entry.get("version", []):
                    if isinstance(v, dict) and v.get("nr") == nr:
                        return v.get("compatibility")
    return None
