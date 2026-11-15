# mkv-forced-subs

Find the **forced subtitle** track in your MKV files and flag it so players show it automatically.

Forced subtitles only translate the parts of a film that aren't in its main language: signs, an alien language, a scene in Italian. Many rips include them as a separate small subtitle track but don't flag it, so you either miss those lines or have to turn the subtitles on by hand. This script finds that track and sets its **default** and **forced** flags with `mkvpropedit`. Only the file header changes, so it takes seconds and never re-encodes anything.

**It only previews by default.** Nothing changes until you add `--apply`, and every change is logged so you can undo it.

## Install

You need **Python 3.8+**, **MediaInfo** (read through `pymediainfo`), and **MKVToolNix** (for `mkvpropedit`, only needed to apply changes).

| | |
|---|---|
| **Windows** | `python -m pip install pymediainfo` (it includes MediaInfo), then install [MKVToolNix](https://mkvtoolnix.download/downloads.html#windows) |
| **macOS** | `brew install media-info mkvtoolnix` then `python3 -m pip install pymediainfo` |
| **Debian / Ubuntu** | `sudo apt install libmediainfo0v5 mkvtoolnix` then `python3 -m pip install pymediainfo` |

Then download `mkv_forced_subs.py` (or clone this repository).

> **Windows tip:** if `pip` isn't recognised, use `python -m pip ...` as shown above.

## Use

```sh
# 1. Preview: see what it would change. Nothing is modified.
python mkv_forced_subs.py "D:\Movies"

# 2. Optionally save a spreadsheet of the results to look through
python mkv_forced_subs.py "D:\Movies" --csv results.csv

# 3. Apply: HIGH-confidence changes are made, MEDIUM ones ask first
python mkv_forced_subs.py "D:\Movies" --apply

# Changed your mind? Every --apply run writes an undo log
python mkv_forced_subs.py --undo mkv_forced_subs_undo_20260101_120000.jsonl --apply
```

You can give several folders or individual `.mkv` files. Folders are searched recursively.

### Example output

```
WILL CHANGE - MEDIUM CONFIDENCE (3)   worth checking first; --apply asks about each one
===================================

Matewan (1987)
       Track  Lang  Lines     Size  Per min  Default  Forced  Title
    >  #4     en       66   716 KB      0.5  -        -
       #5     en     3132  45.3 MB     23.5  -        -
    Why:     #4 has 2% of the lines of the full track (#5)
    Change:  Set default + forced on #4

CHECK MANUALLY (20)   couldn't tell - these are never changed
===================

  No line counts in the file (usually image subtitles) - play the film to check:
    Big Hero 6 (2014)
    Cars (2006)
    ...

SUMMARY   380 files scanned in 1m 16s
=======

     11  to change (8 HIGH, 3 MEDIUM)
     35  already correct
     20  to check manually
    314  with no forced track

Preview only - no files were changed.
```

`>` marks the track the script picked. Add `--verbose` to also see full paths, the exact `mkvpropedit` commands, and the films that need nothing.

## How it decides

For each file it looks at the subtitle tracks in your language (English unless you pass `--lang`) and checks, in order:

| Confidence | When | With `--apply` |
|---|---|---|
| **HIGH** | A track is already flagged forced, or its title says *Forced*, *Foreign* or *Signs* | Applied |
| **MEDIUM** | A track has under 10% of the subtitle lines of the full track (`--max-ratio`) | Asks you first (or `--yes`) |
| **LOW** | Can't tell, e.g. only one track, no language tag, or no line counts in the file | **Never changed**, listed under "Check manually" |

Tracks titled *SDH*, *CC*, *Commentary*, *Hearing Impaired* or *Descriptive* are never picked.

When a forced track is set as default, any **other** subtitle track marked default has that flag cleared, so the player doesn't show full subtitles instead. Use `--keep-other-defaults` to leave them alone.

### Why some files are "check manually"

- **Image-based subtitles (PGS/VobSub) without statistics.** Some files don't store how many subtitle lines a track has, and MediaInfo can't count them, so there's nothing to compare. Play the file and look at the subtitle tracks.
- **Only one subtitle track in your language.** If it's sparse it might be forced, or it might just be a quiet film.
- **Subtitle tracks with no language tag.** They might be in your language.

Check MEDIUM results quickly too, before you apply them. Commentary and lyrics tracks can be small enough to look like forced subtitles. The default 10% cutoff came from testing on a real 380-film library: real forced tracks were all 0–9% of the full track, and the false matches were 14–21%.

## All options

```
python mkv_forced_subs.py --help
```

| Option | What it does |
|---|---|
| `--apply` | Make the changes (HIGH automatically, MEDIUM after asking) |
| `-y`, `--yes` | With `--apply`, don't ask about MEDIUM changes. LOW is still never applied |
| `--lang CODE` | Subtitle language to look for: `en`, `fr`, `de`, `eng`, `french`… (default `en`) |
| `--max-ratio N` | MEDIUM cutoff as a fraction of the full track (default `0.10`) |
| `--max-per-minute N` | With only one track, flag it for checking below this many lines per minute (default `2`) |
| `--keep-other-defaults` | Don't clear the default flag on other subtitle tracks |
| `--csv FILE` | Also save results as a CSV file |
| `--undo-log FILE` | Where `--apply` records the old flags (default: a timestamped file in the current folder) |
| `--undo FILE` | Restore flags from an undo log (previews unless `--apply`) |
| `--mkvpropedit PATH` | Location of `mkvpropedit` if it isn't found automatically |
| `-v`, `--verbose` | Also list films that need nothing, and show full paths and `mkvpropedit` commands |
| `--no-color` | Don't use colours (they're also off when output goes to a file, or if `NO_COLOR` is set) |

## Running the tests

```sh
python -m pip install pytest
python -m pytest
```

The tests use fake track data, so they don't need MediaInfo, MKVToolNix or any video files.

## License

MIT. See [LICENSE](LICENSE).
