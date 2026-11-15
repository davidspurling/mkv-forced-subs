#!/usr/bin/env python3
"""
mkv_forced_subs - find "forced" subtitle tracks in MKV files and flag them.

Forced subtitles are the short subtitle tracks that only translate the parts of
a film in another language (signs, alien speech, a scene in Italian...). Players
show them automatically only if the track is flagged "default" and "forced".

This script looks at every subtitle track in each MKV file, works out which one
is probably the forced track, and (only when you ask with --apply) sets its
flags using mkvpropedit from MKVToolNix. mkvpropedit only edits the file header,
so it's quick and never re-encodes anything.

By default it only PREVIEWS: nothing is changed until you add --apply.
Run with --help for all options.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

# pymediainfo is imported here but checked in main(), so we can show a friendly
# install message instead of a traceback (and so tests can run without it).
try:
    from pymediainfo import MediaInfo
except ImportError:  # pragma: no cover - depends on the environment
    MediaInfo = None

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Defaults (most can be changed with command-line options)
# ---------------------------------------------------------------------------

# A track is "probably forced" when it has less than this fraction of the
# events (or bytes) of the biggest subtitle track in the same language.
# Real forced tracks are usually 0-9%; commentary/lyrics tracks can be 14-21%.
DEFAULT_MAX_RATIO = 0.10

# Full subtitles usually run 8-40 events per minute; forced ones well under 2.
# Used when a file has only one subtitle track in the language, so there is
# nothing to compare it with. Such results are never applied automatically.
DEFAULT_MAX_PER_MINUTE = 2.0

# Words in a track title that mean the track IS forced subtitles.
FORCED_TITLE_WORDS = ("forced", "foreign", "signs")

# Words in a track title that mean the track is NOT forced subtitles.
EXCLUDE_TITLE_WORDS = ("sdh", "commentary", "cc", "hearing impaired", "descriptive")

# mkvpropedit exit codes: 0 = OK, 1 = OK with warnings, 2 = error.
MKVPROPEDIT_MAX_OK_EXIT = 1


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Sub:
    """One subtitle track, with the values the decision logic needs."""
    track_id: object            # MKV track number, as MediaInfo reports it
    selector: str | None        # how mkvpropedit finds it, e.g. 'track:=123...'
    title: str
    lang_code: str              # raw language code, e.g. 'en' ('' if missing)
    language: str               # 'match', 'other' or 'unknown' (vs --lang)
    events: int | None          # number of subtitle lines/images
    size: int | None            # stream size in bytes
    per_minute: float | None    # events per minute of film
    forced: bool
    default: bool
    excluded: bool              # SDH/commentary etc. - never picked as forced


@dataclass
class Result:
    """What the script decided about one file."""
    path: str
    confidence: str | None = None   # 'HIGH', 'MEDIUM', 'LOW' or None (no forced track)
    reason: str = ""
    chosen: Sub | None = None       # the probable forced track
    subs: list = field(default_factory=list)  # every subtitle track in the file
    error: str | None = None        # set if the file couldn't be read
    group: str = ""                 # for LOW results: heading to list it under

    @property
    def matching(self):
        """Subtitle tracks in the language we're looking for."""
        return [s for s in self.subs if s.language == "match"]


# ---------------------------------------------------------------------------
# Helpers for reading MediaInfo values safely (any of them can be missing)
# ---------------------------------------------------------------------------

def to_int(value):
    """Convert a MediaInfo value like '1234' or '1234.000' to int, else None.

    Don't use this for unique IDs: float() rounds numbers that large.
    """
    if value is None:
        return None
    try:
        return int(float(str(value).split()[0]))
    except (ValueError, IndexError):
        return None


def is_yes(value):
    """MediaInfo reports flags as 'Yes'/'No' strings."""
    return str(value).strip().lower() == "yes"


def title_has_word(title, words):
    """True if the title contains any of the words (whole words, any case).

    Non-letters are treated as spaces, so 'English (Forced)' and '[SDH]' match,
    but 'cc' doesn't match inside a word like 'accent'.
    """
    cleaned = "".join(c if c.isalpha() else " " for c in (title or "").lower())
    title_words = cleaned.split()
    joined = " ".join(title_words)
    for word in words:
        if " " in word:                     # multi-word phrase
            if word in joined:
                return True
        elif word in title_words:
            return True
    return False


def language_status(track, wanted):
    """Return 'match', 'other' or 'unknown' for a track vs the wanted language.

    MediaInfo gives a code in `language` ('en', 'en-US') and several spellings
    in `other_language` (['English', 'en', 'eng', ...]), so --lang en, eng and
    english all work.
    """
    names = set()
    code = (track.language or "").lower()
    if code:
        names.add(code.replace("_", "-").split("-")[0])   # 'en-US' -> 'en'
    for name in track.other_language or []:
        names.add(str(name).lower())
    if not names:
        return "unknown"
    return "match" if wanted.lower() in names else "other"


def track_selector(track):
    """How to point mkvpropedit at this track.

    Prefer the unique ID ('track:=UID'), which doesn't depend on track order.
    Kept as a string: these IDs are too big to survive a float conversion.
    Fall back to the MKV track number ('track:@N').
    """
    uid = str(track.unique_id or "").split(" ")[0]
    if uid.isdigit():
        return "track:=" + uid
    if track.track_id is not None:
        return "track:@" + str(track.track_id)
    return None


# ---------------------------------------------------------------------------
# Analysis of one file
# ---------------------------------------------------------------------------

def read_subs(path, wanted_lang):
    """Read a file with MediaInfo and return its subtitle tracks as Sub objects."""
    media_info = MediaInfo.parse(path)

    # Film length in minutes (from the General track), for events per minute.
    minutes = None
    for track in media_info.tracks:
        if track.track_type == "General":
            duration_ms = to_int(track.duration)
            if duration_ms:
                minutes = duration_ms / 60000
            break

    subs = []
    for track in media_info.tracks:
        if track.track_type != "Text":
            continue
        events = to_int(track.count_of_elements)
        subs.append(Sub(
            track_id=track.track_id,
            selector=track_selector(track),
            title=track.title or "",
            lang_code=track.language or "",
            language=language_status(track, wanted_lang),
            events=events,
            size=to_int(track.stream_size),
            per_minute=(events / minutes) if (events is not None and minutes) else None,
            forced=is_yes(track.forced),
            default=is_yes(track.default),
            excluded=title_has_word(track.title, EXCLUDE_TITLE_WORDS),
        ))
    return subs


def smallest(subs):
    """The track with the fewest events (or smallest size if events unknown)."""
    inf = float("inf")
    return min(subs, key=lambda s: (s.events if s.events is not None else inf,
                                    s.size if s.size is not None else inf))


def decide(result, max_ratio, max_per_minute):
    """Fill in result.confidence / reason / chosen from result.subs.

    Checks, strongest first:
      1. a track is already flagged forced              -> HIGH
      2. a track title says Forced / Foreign / Signs    -> HIGH
      3. a track is under max_ratio of the full track   -> MEDIUM
      4. only one track, and very few events per minute -> LOW (check manually)
    """
    matching = result.matching
    candidates = [s for s in matching if not s.excluded]

    def done(confidence, reason, chosen=None, group=""):
        result.confidence, result.reason, result.chosen = confidence, reason, chosen
        result.group = group
        return result

    if not candidates:
        untagged = [s for s in result.subs if s.language == "unknown"]
        if untagged:
            ids = ", ".join("#" + str(s.track_id) for s in untagged)
            return done("LOW", f"track(s) {ids} have no language tag",
                        group="Subtitle tracks with no language tag - they might be yours")
        return done(None, "no subtitle tracks in this language that could be forced")

    # 1. Already flagged forced.
    flagged = [s for s in candidates if s.forced]
    if flagged:
        chosen = smallest(flagged)
        return done("HIGH", f"#{chosen.track_id} is already flagged forced", chosen)

    # 2. Title says it's forced.
    titled = [s for s in candidates if title_has_word(s.title, FORCED_TITLE_WORDS)]
    if titled:
        chosen = smallest(titled)
        return done("HIGH", f"#{chosen.track_id} is titled {chosen.title!r}", chosen)

    # 3. Compare with the full track (the biggest one, SDH included).
    if len(matching) >= 2:
        # Compare by event count if every track has one; otherwise by size in
        # bytes, which also works for image-based (PGS/VobSub) subtitles.
        if all(s.events is not None for s in matching):
            measure, unit = "events", "lines"
        elif all(s.size is not None for s in matching):
            measure, unit = "size", "size"
        else:
            return done("LOW", "no line counts or sizes stored in the file",
                        group="No line counts in the file (usually image subtitles) "
                              "- play the film to check")

        full = max(matching, key=lambda s: getattr(s, measure))
        if getattr(full, measure) == 0:
            return done(None, "subtitle tracks are empty")

        best = min(candidates, key=lambda s: getattr(s, measure))
        if best is full:
            return done(None, "only full-length subtitles found")

        ratio = getattr(best, measure) / getattr(full, measure)
        reason = f"#{best.track_id} has {ratio:.0%} of the {unit} of the full track (#{full.track_id})"
        if ratio < max_ratio:
            return done("MEDIUM", reason, best)
        return done(None, f"smallest track {reason} - too big to be forced")

    # 4. Only one track: judge by events per minute, but never auto-apply.
    only = candidates[0]
    if only.per_minute is not None and only.per_minute < max_per_minute:
        return done("LOW", f"{only.per_minute:.1f} lines/min", only,
                    group="Only one subtitle track and it's sparse - forced, or just a quiet film?")
    return done(None, "only one subtitle track and it looks like full subtitles")


def analyse(path, wanted_lang="en", max_ratio=DEFAULT_MAX_RATIO,
            max_per_minute=DEFAULT_MAX_PER_MINUTE):
    """Read one file and decide which track (if any) is probably forced."""
    result = Result(path)
    try:
        result.subs = read_subs(path, wanted_lang)
    except Exception as error:  # a damaged file shouldn't stop the whole scan
        result.error = str(error) or error.__class__.__name__
        result.group = "Couldn't read the file"
        return result
    decide(result, max_ratio, max_per_minute)
    if result.chosen is not None and result.chosen.selector is None:
        result.confidence = "LOW"
        result.group = "Track has no ID that mkvpropedit can use"
    return result


# ---------------------------------------------------------------------------
# Planning and running mkvpropedit
# ---------------------------------------------------------------------------

def plan_edits(result, keep_other_defaults=False):
    """List the flag changes needed for a file, as [(Sub, {flag: value}), ...].

    Only HIGH and MEDIUM results get edits. The chosen track gets default and
    forced set (only the flags that aren't already set). Unless
    keep_other_defaults is True, any other subtitle track marked default has
    that flag cleared, so players don't show it instead of the forced track.
    """
    if result.confidence not in ("HIGH", "MEDIUM") or result.chosen is None:
        return []
    chosen = result.chosen
    edits = []

    changes = {}
    if not chosen.default:
        changes["flag-default"] = 1
    if not chosen.forced:
        changes["flag-forced"] = 1
    if changes:
        edits.append((chosen, changes))

    if not keep_other_defaults:
        for sub in result.subs:
            if sub is not chosen and sub.default and sub.selector:
                edits.append((sub, {"flag-default": 0}))
    return edits


def command_args(exe, path, edits):
    """Build the mkvpropedit argument list. `edits` is [(selector, {flag: value})]."""
    args = [exe, path]
    for selector, changes in edits:
        args += ["--edit", selector]
        for flag, value in changes.items():
            args += ["--set", f"{flag}={value}"]
    return args


def format_command(args):
    """Turn an argument list into a command line you can paste into a shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def selector_edits(edits):
    """[(Sub, changes)] -> [(selector, changes)] for command_args()."""
    return [(sub.selector, changes) for sub, changes in edits]


def find_mkvpropedit(explicit=None):
    """Locate mkvpropedit: --mkvpropedit, then PATH, then usual install folders."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("mkvpropedit")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(os.path.join(base, "MKVToolNix", "mkvpropedit.exe"))
    elif sys.platform == "darwin":
        candidates += sorted(glob.glob("/Applications/MKVToolNix*.app/Contents/MacOS/mkvpropedit"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def can_write(path):
    """Check we can open the file for writing, without changing it.

    Returns (True, '') or (False, reason). Catches the common problems (file
    open in another program, read-only file, read-only network share) before
    mkvpropedit runs, so the user gets a clear message.
    """
    try:
        with open(path, "r+b"):
            pass
        return True, ""
    except OSError as error:
        return False, error.strerror or str(error)


WRITE_HELP = ("Check the file isn't open in another program (media player, Plex/Jellyfin "
              "scan, Explorer preview), isn't marked read-only, and - on a network share/NAS - "
              "that your account has write permission.")

# Stop applying after this many files in a row can't be written, with nothing
# changed yet: it's almost always a permissions problem that affects them all.
MAX_UNWRITABLE_BEFORE_STOP = 3


def run_mkvpropedit(args):
    """Run mkvpropedit. Returns (ok, message)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except OSError as error:
        return False, str(error)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode > MKVPROPEDIT_MAX_OK_EXIT:
        # mkvpropedit also prints progress ('The file is being analyzed.');
        # keep just the error lines if there are any.
        errors = [line for line in output.splitlines() if line.startswith("Error:")]
        return False, "\n".join(errors) or output or f"exit code {proc.returncode}"
    return True, output


def write_undo_record(undo_file, path, edits):
    """Append the flags as they were BEFORE the change, so --undo can restore them."""
    tracks = []
    for sub, changes in edits:
        before = {}
        for flag in changes:
            before[flag] = int(sub.default) if flag == "flag-default" else int(sub.forced)
        tracks.append({"selector": sub.selector, "track_id": str(sub.track_id), "flags": before})
    record = {"file": path, "changed_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "tracks": tracks}
    with open(undo_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def status_of(result, edits):
    """Group a result for the report."""
    if result.error:
        return "error"
    if result.confidence == "LOW":
        return "check"
    if result.confidence in ("HIGH", "MEDIUM"):
        return "change" if edits else "ok"
    return "none"


class Style:
    """Optional terminal colours. Off when output is redirected, with --no-color,
    or when the NO_COLOR environment variable is set (https://no-color.org)."""

    def __init__(self, enabled=False):
        self.enabled = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text):
        return self._wrap("1", text)

    def dim(self, text):
        return self._wrap("2", text)

    def green(self, text):
        return self._wrap("32", text)

    def yellow(self, text):
        return self._wrap("33", text)

    def red(self, text):
        return self._wrap("31", text)

    def cyan(self, text):
        return self._wrap("36", text)


STYLE = Style()   # replaced in main() once we know where output is going


def colours_supported(no_color_flag):
    """Should we use colour? Also switches on colour support in Windows consoles."""
    if no_color_flag or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:  # enable ANSI escape codes (ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


def film_name(path):
    """A short name for a film.

    'D:\\Movies\\Troy (2004)\\Troy (2004)_HEVC.mkv' -> 'Troy (2004)': when the
    file sits in its own folder and its name starts with the folder's name, the
    folder name is used, which drops tags like '_HEVC'. Otherwise the file name
    without '.mkv'.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if folder and stem.lower().startswith(folder.lower()):
        return folder
    return stem


def human_size(size):
    if size is None:
        return "?"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def describe_changes(edits):
    """[(Sub, changes)] -> 'Set default + forced on #7, clear default on #5'."""
    parts = []
    for sub, changes in edits:
        if changes.get("flag-default") == 0:
            parts.append(f"clear default on #{sub.track_id}")
        else:
            flags = [name for name in ("default", "forced") if changes.get("flag-" + name) == 1]
            parts.append(f"set {' + '.join(flags)} on #{sub.track_id}")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


def track_table(result, edits, indent="    "):
    """Aligned table of the subtitle tracks worth showing, chosen track marked.

    Shows tracks in the wanted language (or with no language), plus any other
    track that will be changed. Columns are padded before colouring so the
    colour codes don't upset the alignment.
    """
    edited = {id(sub) for sub, _ in edits}
    subs = [s for s in result.subs if s.language != "other" or id(s) in edited]
    if not subs:
        return []

    header = ["", "Track", "Lang", "Lines", "Size", "Per min", "Default", "Forced", "Title"]
    right_aligned = {3, 4, 5}       # numeric columns
    rows = []
    for s in subs:
        note = s.title
        if s.excluded:
            note = (note + "  " if note else "") + "(SDH/commentary - ignored)"
        rows.append([
            ">" if s is result.chosen else "",
            f"#{s.track_id}",
            s.lang_code or "?",
            str(s.events) if s.events is not None else "?",
            human_size(s.size),
            f"{s.per_minute:.1f}" if s.per_minute is not None else "",
            "yes" if s.default else "-",
            "yes" if s.forced else "-",
            note,
        ])

    widths = [max(len(row[i]) for row in [header] + rows) for i in range(len(header))]

    def fmt(row):
        cells = [cell.rjust(widths[i]) if i in right_aligned else cell.ljust(widths[i])
                 for i, cell in enumerate(row)]
        return (indent + "  ".join(cells)).rstrip()

    lines = [STYLE.dim(fmt(header))]
    for s, row in zip(subs, rows):
        line = fmt(row)
        lines.append(STYLE.bold(STYLE.cyan(line)) if s is result.chosen else line)
    return lines


def print_film(result, edits, exe_name, verbose=False):
    """Print one film: name, track table, why, and what will change."""
    print(STYLE.bold(film_name(result.path)))
    if verbose:
        print(STYLE.dim("    " + result.path))
    for line in track_table(result, edits):
        print(line)
    print(f"    {STYLE.dim('Why:')}     {result.reason}")
    if edits:
        print(f"    {STYLE.dim('Change:')}  {describe_changes(edits)}")
        if verbose:
            command = format_command(command_args(exe_name, result.path, selector_edits(edits)))
            print(f"    {STYLE.dim('Command:')} {STYLE.dim(command)}")
    print()


def print_heading(title, count=None, note=""):
    """Section heading, e.g. 'WILL CHANGE - HIGH CONFIDENCE (8)'."""
    text = title if count is None else f"{title} ({count})"
    print()
    print(STYLE.bold(text) + (STYLE.dim("   " + note) if note else ""))
    print(STYLE.dim("=" * len(text)))
    print()


def print_check_list(results):
    """'Check manually' films, grouped by why the script couldn't decide."""
    groups = {}
    for result in results:
        groups.setdefault(result.group or "Other", []).append(result)
    for group, members in groups.items():
        print(f"  {group}:")
        # Only show per-film details when they differ (otherwise the heading says it all).
        details = {result.error or result.reason for result in members}
        for result in members:
            detail = (result.error or result.reason) if len(details) > 1 else ""
            print(f"    {film_name(result.path)}" + (STYLE.dim(f"   ({detail})") if detail else ""))
        print()


def format_duration(seconds):
    minutes, seconds = divmod(int(round(seconds)), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class Progress:
    """'[120/380] scanning...' on one line, only when writing to a terminal."""

    def __init__(self, total):
        self.total = total
        self.enabled = sys.stderr.isatty()

    width = 0

    def show(self, index, path=""):
        if self.enabled:
            text = f"Scanning {index}/{self.total}  {film_name(path)}"[:78]
            sys.stderr.write("\r" + text.ljust(self.width))
            self.width = len(text)
            sys.stderr.flush()

    def clear(self):
        if self.enabled:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()


def write_csv(csv_path, rows, exe_name):
    """Write one row per file, for sorting and checking in a spreadsheet."""
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "status", "confidence", "reason", "track", "track_events",
                         "track_size_kb", "command"])
        for result, edits in rows:
            chosen = result.chosen
            writer.writerow([
                result.path,
                status_of(result, edits),
                result.confidence or "",
                result.error or result.reason,
                chosen.track_id if chosen else "",
                chosen.events if chosen and chosen.events is not None else "",
                round(chosen.size / 1024) if chosen and chosen.size is not None else "",
                format_command(command_args(exe_name, result.path, selector_edits(edits))) if edits else "",
            ])


def ask(question):
    """Ask yes/no/quit. Returns 'y', 'n', 'q', or None if there's no terminal."""
    if not sys.stdin.isatty():
        return None
    while True:
        try:
            answer = input(question + " [y/N/q] ").strip().lower()
        except EOFError:
            return None
        if answer in ("y", "yes"):
            return "y"
        if answer in ("", "n", "no"):
            return "n"
        if answer in ("q", "quit"):
            return "q"


# ---------------------------------------------------------------------------
# Command-line entry points
# ---------------------------------------------------------------------------

def find_mkv_files(paths):
    """Every .mkv file in the given files/folders (searched recursively)."""
    found = []
    for path in paths:
        if os.path.isfile(path):
            if path.lower().endswith(".mkv"):
                found.append(path)
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    if name.lower().endswith(".mkv"):
                        found.append(os.path.join(root, name))
        else:
            raise FileNotFoundError(path)
    return found


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mkv_forced_subs",
        description="Find forced subtitle tracks in MKV files and flag them as default + forced. "
                    "Only previews unless you add --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  mkv_forced_subs.py "D:\\Movies"                  preview, change nothing
  mkv_forced_subs.py /media/movies --csv out.csv  preview and save a spreadsheet
  mkv_forced_subs.py /media/movies --apply        apply HIGH, ask about MEDIUM
  mkv_forced_subs.py --undo mkv_forced_subs_undo_20260101_120000.jsonl --apply

confidence levels:
  HIGH    the track is already flagged forced, or its title says Forced/Foreign/Signs
  MEDIUM  the track is much smaller than the full subtitles (see --max-ratio)
  LOW     can't tell - listed under "check manually", never changed
""")
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="MKV files or folders to scan (folders are searched recursively)")
    parser.add_argument("--apply", action="store_true",
                        help="make the changes: HIGH automatically, MEDIUM after asking")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="with --apply, don't ask before MEDIUM changes (LOW is never applied)")
    parser.add_argument("--lang", default="en", metavar="CODE",
                        help="subtitle language to look for, e.g. en, fr, de (default: en)")
    parser.add_argument("--max-ratio", type=float, default=DEFAULT_MAX_RATIO, metavar="N",
                        help="a track is probably forced if it is under this fraction of the "
                             f"full track (default: {DEFAULT_MAX_RATIO})")
    parser.add_argument("--max-per-minute", type=float, default=DEFAULT_MAX_PER_MINUTE, metavar="N",
                        help="with only one track, flag it for checking if under this many "
                             f"events per minute (default: {DEFAULT_MAX_PER_MINUTE})")
    parser.add_argument("--keep-other-defaults", action="store_true",
                        help="don't clear the default flag on other subtitle tracks")
    parser.add_argument("--csv", metavar="FILE", help="also save the results as a CSV file")
    parser.add_argument("--undo-log", metavar="FILE",
                        help="where --apply records the old flags "
                             "(default: mkv_forced_subs_undo_<date>_<time>.jsonl)")
    parser.add_argument("--undo", metavar="FILE",
                        help="restore the flags recorded in an undo log (previews unless --apply)")
    parser.add_argument("--mkvpropedit", metavar="PATH",
                        help="path to mkvpropedit if it isn't on your PATH")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also list films that need nothing, and show full paths and "
                             "mkvpropedit commands")
    parser.add_argument("--no-color", action="store_true", help="don't use colours in the output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args(argv)
    if not args.paths and not args.undo:
        parser.error("give at least one file or folder to scan (or --undo FILE)")
    if args.paths and args.undo:
        parser.error("--undo can't be combined with paths to scan")
    if not 0 < args.max_ratio < 1:
        parser.error("--max-ratio must be between 0 and 1, e.g. 0.10")
    return args


MKVTOOLNIX_HELP = ("mkvpropedit not found. Install MKVToolNix (https://mkvtoolnix.download/), "
                   "e.g. 'sudo apt install mkvtoolnix' or 'brew install mkvtoolnix', "
                   "or give its location with --mkvpropedit PATH.")


def undo_main(args):
    """Restore flags from an undo log written by --apply."""
    try:
        with open(args.undo, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError) as error:
        print(f"Can't read undo log {args.undo}: {error}", file=sys.stderr)
        return 2

    exe = find_mkvpropedit(args.mkvpropedit)
    if args.apply and not exe:
        print(MKVTOOLNIX_HELP, file=sys.stderr)
        return 2

    # Newest first, so a file changed twice ends up as it was originally.
    plans = []
    for record in reversed(records):
        edits = [(t["selector"], t["flags"]) for t in record["tracks"]]
        plans.append((record["file"], edits))
        print(format_command(command_args(exe or "mkvpropedit", record["file"], edits)))

    if not args.apply:
        print(f"\nPreview only - {len(plans)} file(s) would be restored. Add --apply to do it.")
        return 0
    if not args.yes:
        answer = ask(f"\nRestore {len(plans)} file(s)?")
        if answer != "y":
            print("Nothing changed." if answer else "No terminal to ask - add --yes to restore.")
            return 0

    failures = 0
    for path, edits in plans:
        ok, message = run_mkvpropedit(command_args(exe, path, edits))
        print(("restored: " if ok else "FAILED:   ") + path + ("" if ok else f"\n    {message}"))
        failures += not ok
    return 1 if failures else 0


def main(argv=None):
    # Don't crash on unusual characters in titles when the console can't show them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = parse_args(argv)
    global STYLE
    STYLE = Style(colours_supported(args.no_color))
    if args.undo:
        return undo_main(args)

    if MediaInfo is None:
        print("pymediainfo isn't installed. Install it with:\n"
              "    python -m pip install pymediainfo", file=sys.stderr)
        return 2
    if not MediaInfo.can_parse():
        print("pymediainfo is installed but can't find the MediaInfo library. Install it with:\n"
              "    Debian/Ubuntu: sudo apt install libmediainfo0v5\n"
              "    Fedora:        sudo dnf install libmediainfo\n"
              "    macOS:         brew install media-info", file=sys.stderr)
        return 2

    exe = find_mkvpropedit(args.mkvpropedit)
    if args.apply and not exe:
        print(MKVTOOLNIX_HELP, file=sys.stderr)
        return 2
    exe_name = exe or "mkvpropedit"

    try:
        files = find_mkv_files(args.paths)
    except FileNotFoundError as error:
        print(f"Not found: {error}", file=sys.stderr)
        return 2
    if not files:
        print("No .mkv files found.")
        return 0

    # --- Scan -----------------------------------------------------------------
    # Results are collected first and printed grouped by outcome afterwards,
    # with a one-line progress counter in the meantime (terminal only).
    started = time.monotonic()
    rows = []
    progress = Progress(len(files))
    for index, path in enumerate(files, start=1):
        progress.show(index, path)
        result = analyse(path, args.lang, args.max_ratio, args.max_per_minute)
        rows.append((result, plan_edits(result, args.keep_other_defaults)))
    progress.clear()
    elapsed = time.monotonic() - started

    # Sort each film into one group.
    by_status = {"change": [], "ok": [], "check": [], "none": [], "error": []}
    for result, edits in rows:
        by_status[status_of(result, edits)].append((result, edits))
    high = [(r, e) for r, e in by_status["change"] if r.confidence == "HIGH"]
    medium = [(r, e) for r, e in by_status["change"] if r.confidence == "MEDIUM"]
    checks = [r for r, _ in by_status["check"] + by_status["error"]]

    # --- Report ---------------------------------------------------------------
    if high:
        print_heading("WILL CHANGE - HIGH CONFIDENCE", len(high),
                      "applied automatically with --apply")
        for result, edits in high:
            print_film(result, edits, exe_name, args.verbose)

    if medium:
        print_heading("WILL CHANGE - MEDIUM CONFIDENCE", len(medium),
                      "worth checking first; --apply asks about each one")
        for result, edits in medium:
            print_film(result, edits, exe_name, args.verbose)

    if checks:
        print_heading("CHECK MANUALLY", len(checks), "couldn't tell - these are never changed")
        print_check_list(checks)

    if args.verbose:
        for key, title in (("ok", "ALREADY CORRECT"), ("none", "NO FORCED TRACK")):
            if by_status[key]:
                print_heading(title, len(by_status[key]))
                for result, _ in by_status[key]:
                    print(f"  {film_name(result.path)}" + STYLE.dim(f"   ({result.reason})"))

    # --- Summary --------------------------------------------------------------
    print_heading("SUMMARY", note=f"{len(files)} files scanned in {format_duration(elapsed)}")
    summary = [
        (len(by_status["change"]), f"to change ({len(high)} HIGH, {len(medium)} MEDIUM)", STYLE.green),
        (len(by_status["ok"]), "already correct", str),
        (len(checks), "to check manually", STYLE.yellow),
        (len(by_status["none"]), "with no forced track", str),
    ]
    for count, label, colour in summary:
        line = f"  {count:5}  {label}"
        print(colour(line) if count else line)

    if args.csv:
        write_csv(args.csv, rows, exe_name)
        print(f"\nCSV saved to {args.csv}")

    changes = high + medium
    if not args.apply:
        print("\n" + STYLE.bold("Preview only - no files were changed."))
        if changes:
            print("Run again with --apply to make these changes.")
        if not exe:
            print(STYLE.dim("Note: " + MKVTOOLNIX_HELP))
        return 0

    # --- Apply ----------------------------------------------------------------
    if not changes:
        return 0
    undo_file = args.undo_log or datetime.datetime.now().strftime(
        "mkv_forced_subs_undo_%Y%m%d_%H%M%S.jsonl")
    applied = skipped = failed = 0
    unwritable_in_a_row = 0
    print_heading("APPLYING CHANGES", len(changes))
    for result, edits in changes:
        name = film_name(result.path)

        # Check write access first, so we don't ask about a change we can't make.
        writable, why = can_write(result.path)
        if not writable:
            print(STYLE.red(f"  FAILED   {name}") + f"   can't write to the file ({why})")
            failed += 1
            unwritable_in_a_row += 1
            if applied == 0 and unwritable_in_a_row >= MAX_UNWRITABLE_BEFORE_STOP:
                print("\n" + STYLE.bold(f"Stopping: the first {unwritable_in_a_row} files "
                                        "couldn't be written to, so the rest probably can't either."))
                print(WRITE_HELP)
                break
            continue
        unwritable_in_a_row = 0

        if result.confidence == "MEDIUM" and not args.yes:
            print_film(result, edits, exe_name, args.verbose)
            answer = ask("    Apply this change?")
            if answer is None:
                print(STYLE.yellow(f"  skipped  {name}") +
                      STYLE.dim("   (no terminal to ask - use --yes to apply MEDIUM changes)"))
                skipped += 1
                continue
            if answer == "q":
                print("Stopping.")
                break
            if answer == "n":
                print(STYLE.yellow(f"  skipped  {name}"))
                skipped += 1
                continue

        ok, message = run_mkvpropedit(command_args(exe, result.path, selector_edits(edits)))
        if ok:
            write_undo_record(undo_file, result.path, edits)
            print(STYLE.green(f"  changed  {name}"))
            applied += 1
        else:
            print(STYLE.red(f"  FAILED   {name}") + f"\n           {message}")
            failed += 1

    print(f"\n{applied} changed, {skipped} skipped, {failed} failed.")
    if failed and unwritable_in_a_row < MAX_UNWRITABLE_BEFORE_STOP:
        print(STYLE.dim(WRITE_HELP))
    if applied:
        print(f"Undo log: {undo_file}")
        print(f"To undo:  {os.path.basename(sys.argv[0]) or 'mkv_forced_subs.py'} "
              f"--undo {undo_file} --apply")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
