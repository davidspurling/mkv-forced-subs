"""
Tests for mkv_forced_subs, using fake MediaInfo data - no real MKV files,
MediaInfo library or MKVToolNix needed.

Run with:  python -m pytest
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mkv_forced_subs as m  # noqa: E402


# ---------------------------------------------------------------------------
# Fake MediaInfo
# ---------------------------------------------------------------------------

def track(kind, **values):
    """A fake MediaInfo track; any attribute not given is None, like pymediainfo."""
    base = dict(track_type=kind, track_id=None, unique_id=None, language=None,
                other_language=None, title=None, forced="No", default="No",
                count_of_elements=None, stream_size=None, duration=None)
    base.update(values)
    return SimpleNamespace(**base)


def general(minutes=120):
    return track("General", duration=str(minutes * 60000))


def sub(track_id, lang="en", **values):
    """A fake subtitle track. lang=None means no language tag."""
    names = {"en": ["English", "English", "en", "eng", "en"],
             "fr": ["French", "French", "fr", "fre", "fr"]}
    return track("Text", track_id=track_id, unique_id=str(1000 + track_id),
                 language=lang, other_language=names.get(lang), **values)


class FakeMediaInfo:
    """Stands in for pymediainfo.MediaInfo; returns tracks by file name."""
    files = {}

    @classmethod
    def parse(cls, path):
        tracks = cls.files[os.path.basename(path)]
        if isinstance(tracks, Exception):
            raise tracks
        return SimpleNamespace(tracks=tracks)

    @staticmethod
    def can_parse():
        return True


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Create empty .mkv files and serve fake track data for them."""
    monkeypatch.setattr(m, "MediaInfo", FakeMediaInfo)

    def make(**files):
        FakeMediaInfo.files = {}
        for name, tracks in files.items():
            filename = name + ".mkv"
            (tmp_path / filename).write_bytes(b"")
            FakeMediaInfo.files[filename] = tracks
        return tmp_path

    return make


def analyse(folder, name, **kwargs):
    return m.analyse(str(folder / (name + ".mkv")), **kwargs)


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def test_already_flagged_is_high_and_needs_nothing(library):
    folder = library(film=[general(), sub(3, count_of_elements=1500),
                           sub(4, count_of_elements=60, forced="Yes", default="Yes")])
    result = analyse(folder, "film")
    assert (result.confidence, result.chosen.track_id) == ("HIGH", 4)
    assert m.plan_edits(result) == []


def test_title_says_forced(library):
    folder = library(film=[general(), sub(3, count_of_elements=1500, default="Yes"),
                           sub(4, title="English (Forced)", count_of_elements=700)])
    result = analyse(folder, "film")
    assert (result.confidence, result.chosen.track_id) == ("HIGH", 4)
    edits = m.selector_edits(m.plan_edits(result))
    assert edits == [("track:=1004", {"flag-default": 1, "flag-forced": 1}),
                     ("track:=1003", {"flag-default": 0})]


def test_cc_inside_a_word_is_not_excluded():
    assert not m.title_has_word("Accented", m.EXCLUDE_TITLE_WORDS)
    assert m.title_has_word("English [CC]", m.EXCLUDE_TITLE_WORDS)
    assert m.title_has_word("Hearing Impaired", m.EXCLUDE_TITLE_WORDS)


def test_small_track_is_medium(library):
    folder = library(film=[general(), sub(3, count_of_elements=1400), sub(4, count_of_elements=90)])
    result = analyse(folder, "film")
    assert (result.confidence, result.chosen.track_id) == ("MEDIUM", 4)


def test_track_over_ratio_is_not_picked(library):
    # 21% - like a lyrics/commentary track; under the default 10% cutoff it's ignored
    folder = library(film=[general(), sub(3, count_of_elements=5000), sub(4, count_of_elements=1050)])
    assert analyse(folder, "film").confidence is None
    assert analyse(folder, "film", max_ratio=0.25).confidence == "MEDIUM"


def test_size_used_when_event_count_missing(library):
    folder = library(film=[general(), sub(3, stream_size=30_000_000), sub(4, stream_size=900_000)])
    result = analyse(folder, "film")
    assert (result.confidence, result.chosen.track_id) == ("MEDIUM", 4)
    assert "size" in result.reason


def test_no_stats_means_check_manually(library):
    folder = library(film=[general(), sub(3), sub(4)])
    result = analyse(folder, "film")
    assert result.confidence == "LOW"
    assert m.plan_edits(result) == []


def test_sdh_is_never_picked(library):
    folder = library(film=[general(), sub(3, title="SDH", count_of_elements=80),
                           sub(4, count_of_elements=1500)])
    assert analyse(folder, "film").confidence is None


def test_single_sparse_track_is_low(library):
    folder = library(film=[general(), sub(3, count_of_elements=100)])
    result = analyse(folder, "film")
    assert result.confidence == "LOW"
    assert m.plan_edits(result) == []


def test_single_full_track_is_none(library):
    folder = library(film=[general(), sub(3, count_of_elements=1300)])
    assert analyse(folder, "film").confidence is None


def test_untagged_language_is_low(library):
    folder = library(film=[general(), sub(3, lang=None, count_of_elements=50)])
    assert analyse(folder, "film").confidence == "LOW"


def test_other_language(library):
    folder = library(film=[general(), sub(3, lang="fr", count_of_elements=1400),
                           sub(4, lang="fr", count_of_elements=40),
                           sub(5, count_of_elements=30)])
    result = analyse(folder, "film", wanted_lang="fr")
    assert result.chosen.track_id == 4
    assert analyse(folder, "film", wanted_lang="fre").chosen.track_id == 4


def test_unreadable_file_is_reported(library):
    folder = library(broken=RuntimeError("bad file"))
    result = analyse(folder, "broken")
    assert result.error == "bad file"
    assert m.status_of(result, []) == "error"


# ---------------------------------------------------------------------------
# Edits and commands
# ---------------------------------------------------------------------------

def test_large_unique_id_is_kept_exactly():
    t = track("Text", unique_id="12585404823333653067", track_id=9)
    assert m.track_selector(t) == "track:=12585404823333653067"


def test_falls_back_to_track_number():
    assert m.track_selector(track("Text", track_id=5)) == "track:@5"


def test_keep_other_defaults(library):
    folder = library(film=[general(), sub(3, count_of_elements=1500, default="Yes"),
                           sub(4, count_of_elements=20)])
    result = analyse(folder, "film")
    assert len(m.plan_edits(result)) == 2
    assert len(m.plan_edits(result, keep_other_defaults=True)) == 1


def test_command_args():
    args = m.command_args("mkvpropedit", "My Film.mkv", [("track:=1", {"flag-forced": 1})])
    assert args == ["mkvpropedit", "My Film.mkv", "--edit", "track:=1", "--set", "flag-forced=1"]


# ---------------------------------------------------------------------------
# Whole program: preview, apply, undo
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_mkvpropedit(monkeypatch):
    """Record mkvpropedit calls instead of running it."""
    calls = []
    monkeypatch.setattr(m, "find_mkvpropedit", lambda explicit=None: "mkvpropedit")
    monkeypatch.setattr(m, "run_mkvpropedit", lambda args: (calls.append(args), (True, ""))[1])
    return calls


def two_films(library):
    return library(
        high=[general(), sub(3, count_of_elements=1500, default="Yes"),
              sub(4, title="Forced", count_of_elements=50)],
        medium=[general(), sub(3, count_of_elements=1500), sub(4, count_of_elements=40)],
    )


def test_preview_changes_nothing(library, fake_mkvpropedit, capsys):
    folder = two_films(library)
    assert m.main([str(folder)]) == 0
    assert fake_mkvpropedit == []
    assert "Preview only" in capsys.readouterr().out


def test_apply_without_terminal_skips_medium(library, fake_mkvpropedit, monkeypatch, tmp_path):
    folder = two_films(library)
    monkeypatch.setattr(m, "ask", lambda question: None)   # no terminal
    undo = tmp_path / "undo.jsonl"
    assert m.main([str(folder), "--apply", "--undo-log", str(undo)]) == 0
    assert len(fake_mkvpropedit) == 1
    assert fake_mkvpropedit[0][1].endswith("high.mkv")


def test_apply_yes_then_undo(library, fake_mkvpropedit, tmp_path):
    folder = two_films(library)
    undo = tmp_path / "undo.jsonl"
    assert m.main([str(folder), "--apply", "--yes", "--undo-log", str(undo)]) == 0
    assert len(fake_mkvpropedit) == 2

    records = [json.loads(line) for line in undo.read_text().splitlines()]
    high = next(r for r in records if r["file"].endswith("high.mkv"))
    assert high["tracks"] == [
        {"selector": "track:=1004", "track_id": "4", "flags": {"flag-default": 0, "flag-forced": 0}},
        {"selector": "track:=1003", "track_id": "3", "flags": {"flag-default": 1}},
    ]

    fake_mkvpropedit.clear()
    assert m.main(["--undo", str(undo), "--apply", "--yes"]) == 0
    restore = next(a for a in fake_mkvpropedit if a[1].endswith("high.mkv"))
    assert restore[2:] == ["--edit", "track:=1004", "--set", "flag-default=0", "--set", "flag-forced=0",
                           "--edit", "track:=1003", "--set", "flag-default=1"]


def test_unwritable_files_stop_early(library, fake_mkvpropedit, monkeypatch, capsys):
    # e.g. a network share that's read-only for this account
    films = {f"film{i}": [general(), sub(3, count_of_elements=1500),
                          sub(4, title="Forced", count_of_elements=50)] for i in range(5)}
    folder = library(**films)
    monkeypatch.setattr(m, "can_write", lambda path: (False, "Permission denied"))
    assert m.main([str(folder), "--apply", "--yes"]) == 1
    out = capsys.readouterr().out
    assert fake_mkvpropedit == []                 # mkvpropedit never run
    assert out.count("FAILED") == 3               # stopped after 3, didn't try all 5
    assert "Stopping" in out and "write permission" in out


def test_csv_output(library, fake_mkvpropedit, tmp_path):
    folder = two_films(library)
    out = tmp_path / "out.csv"
    m.main([str(folder), "--csv", str(out)])
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("file,status,confidence")
    assert text.count("change") == 2
