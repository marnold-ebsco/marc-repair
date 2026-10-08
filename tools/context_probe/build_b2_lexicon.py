"""Build the JSON lexicon `marc_repair.py --b2-lexicon` loads, end to end.

Orchestrates the steps documented in HANDOFF.md's "HOWTO: build the
`--b2-lexicon` file": scan a corpus for cleanly-encoded diacritic words
(`lang_lexicon_probe.py build`), optionally pull in outside German/LC word
lists for coverage a single corpus can't provide on its own
(`lc-words`/`merge`), then trim the result down to the small a/o/u-umlaut-only
JSON file the fixer actually reads (`trim`). Each step's own function in
`lang_lexicon_probe.py` is called directly, not shelled out to -- this file
only adds the orchestration, downloading, and idempotency on top.

Every intermediate artifact (base/merged pickles, downloaded word lists,
extracted .deb contents) is written under `--workdir` and, like everything
produced by this script, is expected to live under the gitignored `working/`
tree -- never commit the output JSON or any intermediate: the German
`wngerman` word list is GPL-2+ and the Library of Congress downloads, while
free, haven't had their redistribution terms checked either (see HANDOFF.md).
Each step is skipped if its output file already exists, so re-running this
after an interruption (the 2.6 GB LC names dump in particular) resumes rather
than restarting; pass --force to rebuild everything from scratch.

Usage:
    python tools/context_probe/build_b2_lexicon.py \\
        --corpus working/GTU_bibs.mrc --out working/b2_lexicon.json

Corpus-only (skip the outside word lists -- thinner, but nothing to
download):
    python tools/context_probe/build_b2_lexicon.py --corpus-only

Include the 2.6 GB LC names dump too (off by default -- see --include-lc-names):
    python tools/context_probe/build_b2_lexicon.py --include-lc-names
"""
from __future__ import annotations

import argparse
import glob
import gzip
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lang_lexicon_probe as lex  # noqa: E402  (same directory, not a package)

LC_SUBJECTS_URL = 'https://id.loc.gov/download/authorities/subjects.skosrdf.nt.gz'
LC_NAMES_URL = 'https://id.loc.gov/download/authorities/names.skosrdf.nt.gz'

#: Chunk size for the manual download loop -- large enough to not dominate
#: runtime with per-chunk overhead, small enough to report progress and to
#: resume close to where an interruption left off.
_DOWNLOAD_CHUNK = 1024 * 1024


def _step(msg: str) -> None:
    print(f'--- {msg}', file=sys.stderr)


def _download(url: str, dest: str, *, force: bool = False) -> None:
    """Download `url` to `dest`, resuming a partial `dest + '.part'` left by
    an earlier interrupted run instead of restarting -- the LC names dump
    (~2.6 GB) is large enough that restarting from zero on every retry is
    its own problem. Skipped entirely if `dest` already exists and not
    `force`."""
    if os.path.exists(dest) and not force:
        _step(f'{dest} already present, skipping download')
        return
    tmp = dest + '.part'
    resume_from = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url)
    if resume_from:
        req.add_header('Range', f'bytes={resume_from}-')
        _step(f'resuming download of {url} at byte {resume_from}')
    else:
        _step(f'downloading {url}')
    with urllib.request.urlopen(req) as resp:
        mode = 'ab'
        if resume_from and resp.status != 206:
            # server ignored the Range header -- it would send the whole
            # file again, so start the local copy over rather than append
            # a second copy onto the front of what's already there.
            _step('server did not honor resume -- restarting download')
            resume_from = 0
            mode = 'wb'
        written = resume_from
        with open(tmp, mode) as fh:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                print(f'\r  {written / 1_000_000:.0f} MB', end='', file=sys.stderr)
    print(file=sys.stderr)
    with gzip.open(tmp, 'rb') as fh:
        fh.read(1)  # raises if the download is truncated/corrupt
    os.replace(tmp, dest)


def _download_wngerman(workdir: str) -> str:
    """apt-get download + dpkg -x the Debian `wngerman` package (without
    installing it system-wide) and return the path to its extracted word
    list. Requires `apt-get`/`dpkg` (Debian/Ubuntu) and that `apt-get
    update` has been run at some point so the package is in the local
    cache -- a stale/missing cache surfaces as a clear subprocess error
    here rather than a silent empty word list."""
    deb_glob = os.path.join(workdir, 'wngerman_*.deb')
    existing = glob.glob(deb_glob)
    if not existing:
        _step('apt-get download wngerman')
        try:
            subprocess.run(
                ['apt-get', 'download', 'wngerman'], cwd=workdir, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(
                'apt-get download wngerman failed -- if the package isn\'t '
                'in the local apt cache, run `sudo apt-get update` first, or '
                'download the .deb manually from '
                'https://packages.debian.org/search?keywords=wngerman and '
                f'place it in {workdir}/'
            ) from exc
        existing = glob.glob(deb_glob)
    deb_path = existing[0]
    extract_dir = os.path.join(workdir, 'wngerman_extracted')
    wordlist_glob = os.path.join(extract_dir, 'usr', 'share', 'dict', '*')
    found = [p for p in glob.glob(wordlist_glob) if os.path.isfile(p)]
    if not found:
        _step(f'dpkg -x {deb_path}')
        subprocess.run(['dpkg', '-x', deb_path, extract_dir], check=True)
        found = [p for p in glob.glob(wordlist_glob) if os.path.isfile(p)]
    if not found:
        raise RuntimeError(
            f'extracted {deb_path} but found no word list under '
            f'{extract_dir}/usr/share/dict/ -- inspect the extracted '
            'package by hand'
        )
    return found[0]


def _lc_wordlist(workdir: str, url: str, label: str) -> str:
    nt_gz = os.path.join(workdir, f'{label}.skosrdf.nt.gz')
    words_txt = os.path.join(workdir, f'{label}_words.txt')
    if not os.path.exists(words_txt):
        _download(url, nt_gz)
        _step(f'lang_lexicon_probe lc-words --nt-gz {nt_gz}')
        lex.lc_words(nt_gz, words_txt)
    else:
        _step(f'{words_txt} already present, skipping')
    return words_txt


def _merge_step(base: str, wordlist: str, out: str) -> str:
    if not os.path.exists(out):
        _step(f'merge {os.path.basename(wordlist)} into {os.path.basename(base)}')
        lex.merge_wordlist(base, wordlist, out)
    else:
        _step(f'{out} already present, skipping merge')
    return out


def build_lexicon(
    corpus: str,
    workdir: str,
    out: str,
    *,
    corpus_only: bool = False,
    include_wngerman: bool = True,
    include_lc_subjects: bool = True,
    include_lc_names: bool = False,
    force: bool = False,
) -> None:
    os.makedirs(workdir, exist_ok=True)

    base_cache = os.path.join(workdir, 'lexicon_base.pkl')
    if force or not os.path.exists(base_cache):
        _step(f'lang_lexicon_probe build --corpus {corpus}')
        lex.build(corpus, base_cache)
    else:
        _step(f'{base_cache} already present, skipping build')

    merged = base_cache
    if not corpus_only:
        if include_wngerman:
            merge_out = os.path.join(workdir, 'lexicon_de.pkl')
            if force or not os.path.exists(merge_out):
                wordlist = _download_wngerman(workdir)
                merged = _merge_step(merged, wordlist, merge_out)
            else:
                _step(f'{merge_out} already present, skipping wngerman fetch/merge')
                merged = merge_out
        if include_lc_subjects:
            merge_out = os.path.join(workdir, 'lexicon_de_sub.pkl')
            if force or not os.path.exists(merge_out):
                wordlist = _lc_wordlist(workdir, LC_SUBJECTS_URL, 'subjects')
                merged = _merge_step(merged, wordlist, merge_out)
            else:
                _step(f'{merge_out} already present, skipping LC subjects fetch/merge')
                merged = merge_out
        if include_lc_names:
            merge_out = os.path.join(workdir, 'lexicon_de_sub_names.pkl')
            if force or not os.path.exists(merge_out):
                wordlist = _lc_wordlist(workdir, LC_NAMES_URL, 'names')
                merged = _merge_step(merged, wordlist, merge_out)
            else:
                _step(f'{merge_out} already present, skipping LC names fetch/merge')
                merged = merge_out

    _step(f'lang_lexicon_probe trim --base {merged} --out {out}')
    lex.trim_for_b2(merged, out)
    _step(f'done -- {out}')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--corpus', default=lex.CORPUS,
                    help=f'MARC file to scan for clean diacritic words (default: {lex.CORPUS})')
    ap.add_argument('--workdir', default='working/outside_lex',
                    help='where intermediate pickles/downloads are kept (default: working/outside_lex)')
    ap.add_argument('--out', default='working/b2_lexicon.json',
                    help='final JSON lexicon path for --b2-lexicon (default: working/b2_lexicon.json)')
    ap.add_argument('--corpus-only', action='store_true',
                    help="skip all outside word lists -- thinner coverage, nothing to download")
    ap.add_argument('--no-wngerman', dest='wngerman', action='store_false',
                    help='skip the Debian wngerman word list')
    ap.add_argument('--no-lc-subjects', dest='lc_subjects', action='store_false',
                    help='skip the LC subjects authority dump (~100 MB)')
    ap.add_argument('--include-lc-names', action='store_true',
                    help='also pull in the LC names authority dump (~2.6 GB) -- off by default')
    ap.add_argument('--force', action='store_true',
                    help='rebuild every step even if its output already exists')
    args = ap.parse_args()
    build_lexicon(
        args.corpus, args.workdir, args.out,
        corpus_only=args.corpus_only,
        include_wngerman=args.wngerman,
        include_lc_subjects=args.lc_subjects,
        include_lc_names=args.include_lc_names,
        force=args.force,
    )


if __name__ == '__main__':
    main()
