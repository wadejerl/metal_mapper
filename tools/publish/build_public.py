#!/usr/bin/env python3
"""build_public.py — build and extend the `public` snapshot branch.

The development journal (main) stays private; what goes to GitHub is the
`public` branch: one commit per line of milestones.txt, whose tree is that
journal commit's tree minus exclude.txt, with rewrite.txt's substitutions
applied, plus LICENSE. Pure git plumbing on a private temporary index — the
real index and every other branch are never touched, and nothing is pushed.
The one working-tree write is --append / --recut writing its line to
milestones.txt (only once the snapshot exists), which you then commit.

    build_public.py                    build missing snapshots, audit the rest
    build_public.py --append REV TAG MESSAGE...
                                       snapshot REV onto the tip as TAG, and
                                       record the line in milestones.txt
    build_public.py --recut REV TAG [MESSAGE...]
                                       redo the LAST milestone from REV before
                                       it has been pushed: drop its tag, move
                                       the branch back, snapshot again, update
                                       its line (message kept unless given)
    build_public.py --check            audit every commit on `public`
    build_public.py --force            delete the branch + milestone tags and
                                       rebuild (moves tags: needs a force-push)
    --no-rewrites                      go on without rewrite.txt (its string
                                       scrub AND its audit are then off)

A milestone whose tag already exists is left alone (it may already be on
GitHub) and only audited; missing ones are appended in order onto the
current tip — including on top of commits pulled back from GitHub. Author,
committer and dates are fixed (dates come from the journal commit), so a
fresh --force rebuild reproduces byte-identical commits as long as the
inputs are unchanged.
"""
import fnmatch
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AUTHOR_NAME = 'Jeremy Wade'
AUTHOR_EMAIL = 'wadejer@procyon.com'
BRANCH = 'public'
REMOTE = os.environ.get('MM_PUBLIC_REMOTE', 'github')          # as pull_public.sh
RBRANCH = os.environ.get('MM_PUBLIC_REMOTE_BRANCH', 'main')
BIG_BLOB = 50 * 1024 * 1024        # GitHub warns at 50 MB, refuses at 100 MB

HERE = Path(__file__).resolve().parent
MILESTONES = HERE / 'milestones.txt'
EXCLUDES = HERE / 'exclude.txt'
REWRITES = HERE / 'rewrite.txt'


def die(msg):
    sys.exit(f'build_public: {msg}')


def git(*args, env=None, input=None, check=True):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run(['git', *args], cwd=ROOT, env=e, input=input,
                       text=True, capture_output=True)
    if check and r.returncode:
        die(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ''


def git_bytes(*args, input=None):
    r = subprocess.run(['git', *args], cwd=ROOT, input=input, capture_output=True)
    if r.returncode:
        die(f"git {' '.join(args)} failed:\n{r.stderr.decode(errors='replace').strip()}")
    return r.stdout


ROOT = Path(subprocess.run(['git', 'rev-parse', '--show-toplevel'], text=True,
                           capture_output=True, check=True).stdout.strip())


# ── inputs ─────────────────────────────────────────────────────────────────
def uncommented(path):
    """Non-blank lines that do not START with '#'. A '#' inside a line is
    data: a milestone message like 'fix #12' must round-trip intact."""
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith('#'):
            yield ln


def parse_milestones():
    out = []
    for ln in uncommented(MILESTONES):
        parts = ln.split(None, 2)
        if len(parts) != 3:
            die(f'milestones.txt: bad line {ln!r} (want: <commit> <tag> <message>)')
        out.append(tuple(parts))
    tags = [t for _, t, _ in out]
    if len(set(tags)) != len(tags):
        die('milestones.txt: duplicate tag')
    return out


def parse_excludes():
    rules = []
    for ln in uncommented(EXCLUDES):
        parts = ln.split(None, 1)
        if len(parts) == 2 and parts[0].endswith('+'):
            rules.append((parts[0][:-1], parts[1].strip()))
        else:
            rules.append((None, ln))
    return rules


def parse_rewrites(allow_missing=False):
    """[(path_pattern, from, to)] — byte substitutions in matching blobs."""
    rules = []
    if not REWRITES.exists():
        if not allow_missing:
            die(f'{REWRITES.relative_to(ROOT)} is missing — without it the older '
                f'snapshots are neither scrubbed nor audited for the strings it '
                f'names. It is tracked on main and deliberately absent from '
                f'{BRANCH}: run from a main checkout, or pass --no-rewrites to go '
                f'on with that scrub and its audit OFF.')
        print('build_public: WARNING: --no-rewrites — string scrub and its audit '
              'are OFF', file=sys.stderr)
        return rules
    for ln in uncommented(REWRITES):
        if ' => ' not in ln:
            die(f'rewrite.txt: bad line {ln!r} (want: <pattern> <from> => <to>)')
        left, to = ln.split(' => ', 1)
        parts = left.split(None, 1)
        if len(parts) != 2:
            die(f'rewrite.txt: bad line {ln!r} (want: <pattern> <from> => <to>)')
        rules.append((parts[0], parts[1].strip().encode(), to.strip().encode()))
    return rules


def rules_for(index, tags, rules):
    """Exclude patterns for the milestone at position `index` (len(tags) =
    beyond the last listed one, i.e. --append or a foreign commit)."""
    pats = []
    for frm, pat in rules:
        if frm is not None:
            if frm not in tags:
                die(f'exclude.txt: "{frm}+" names a tag not in milestones.txt')
            if index < tags.index(frm):
                continue
        pats.append(pat)
    return pats


def matches(path, pat):
    if pat.endswith('/'):
        return path.startswith(pat)
    if any(c in pat for c in '*?['):
        return fnmatch.fnmatchcase(path, pat)
    return path == pat or path.startswith(pat + '/')


# ── trees and commits ──────────────────────────────────────────────────────
def ls_tree(rev):
    """[(mode, type, sha, size, path)] for the whole tree of rev."""
    out = []
    for ent in git('ls-tree', '-r', '-l', '-z', '--full-tree', rev).split('\0'):
        if not ent:
            continue
        meta, path = ent.split('\t', 1)
        mode, typ, sha, size = meta.split()
        out.append((mode, typ, sha, -1 if size == '-' else int(size), path))
    return out


def rewrite_blob(sha, path, rewrites):
    """New blob sha after applying the rewrite rules matching path, or None."""
    rules = [(f, t) for pat, f, t in rewrites if matches(path, pat)]
    if not rules:
        return None
    data = git_bytes('cat-file', 'blob', sha)
    new = data
    for f, t in rules:
        new = new.replace(f, t)
    if new == data:
        return None
    return git_bytes('hash-object', '-w', '--stdin', input=new).decode().strip()


def snapshot_tree(src, pats, rewrites, license_blob):
    """Tree of src minus pats, rewritten, plus LICENSE.
    Returns (tree, removed_paths, rewritten_paths)."""
    keep, removed, rewritten = [], [], []
    for mode, typ, sha, _size, path in ls_tree(src):
        if typ != 'blob':
            die(f'{src[:9]}: {path} is a {typ} (submodule?) — not handled')
        if path == 'LICENSE' or any(matches(path, p) for p in pats):
            removed.append(path)
            continue
        new = rewrite_blob(sha, path, rewrites)
        if new:
            sha = new
            rewritten.append(path)
        keep.append(f'{mode} {sha}\t{path}')
    keep.append(f'100644 {license_blob}\tLICENSE')
    fd, idx = tempfile.mkstemp(prefix='public-index.')
    os.close(fd)
    os.unlink(idx)                      # update-index wants to create it
    try:
        env = {'GIT_INDEX_FILE': idx}
        git('update-index', '--index-info', env=env, input='\n'.join(keep) + '\n')
        return git('write-tree', env=env), removed, rewritten
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def identity_env(date):
    return {'GIT_AUTHOR_NAME': AUTHOR_NAME, 'GIT_AUTHOR_EMAIL': AUTHOR_EMAIL,
            'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_NAME': AUTHOR_NAME,
            'GIT_COMMITTER_EMAIL': AUTHOR_EMAIL, 'GIT_COMMITTER_DATE': date}


def make_commit(tree, parent, tag, msg, date):
    body = (f'{tag}: {msg}\n\n'
            f'Snapshot of the Metal Mapper development tree as of {date[:10]}.\n')
    args = ['commit-tree', tree] + (['-p', parent] if parent else [])
    return git(*args, env=identity_env(date), input=body)


def audit(commit, pats, rewrites):
    """Problems with a public commit: excluded paths present, rewrite
    targets still present, missing LICENSE, blobs over BIG_BLOB.
    Returns (problems, n_files, total_bytes)."""
    problems, n, total = [], 0, 0
    seen_license = False
    for _mode, _typ, sha, size, path in ls_tree(commit):
        n += 1
        total += max(size, 0)
        if path == 'LICENSE':
            seen_license = True
        if any(matches(path, p) for p in pats):
            problems.append(f'excluded path present: {path}')
        if size > BIG_BLOB:
            problems.append(f'{size / 1048576:.0f} MB blob: {path}')
        froms = [f for pat, f, _t in rewrites if matches(path, pat)]
        if froms:
            data = git_bytes('cat-file', 'blob', sha)
            for f in froms:
                if f in data:
                    problems.append(f'{path} still contains {f.decode(errors="replace")!r}')
    if not seen_license:
        problems.append('no LICENSE')
    return problems, n, total


def tag_commit(tag):
    return git('rev-parse', '-q', '--verify', f'refs/tags/{tag}^{{commit}}', check=False)


def branch_tip():
    return git('rev-parse', '-q', '--verify', f'refs/heads/{BRANCH}^{{commit}}', check=False)


def is_ancestor(a, b):
    return not subprocess.run(['git', 'merge-base', '--is-ancestor', a, b], cwd=ROOT).returncode


def parents(commit):
    return git('rev-list', '--parents', '-n1', commit).split()[1:]


def advance(old, new):
    """refs/heads/BRANCH: old -> new, compare-and-swap ('' = does / must not exist)."""
    if old == new:
        return
    if new:
        git('update-ref', f'refs/heads/{BRANCH}', new, old or '')
    else:
        git('update-ref', '-d', f'refs/heads/{BRANCH}', old)


def milestone_index(commit, tags):
    """Position of the milestone whose tag points at commit; len(tags) for
    a commit that is not a milestone (pulled back from GitHub)."""
    for t in git('tag', '--points-at', commit).split():
        if t in tags:
            return tags.index(t)
    return len(tags)


# ── modes ──────────────────────────────────────────────────────────────────
def check_only(rules, rewrites, tags):
    if not branch_tip():
        die(f'no {BRANCH} branch')
    bad = 0
    for c in git('rev-list', '--first-parent', '--reverse', BRANCH).split():
        pats = rules_for(milestone_index(c, tags), tags, rules)
        problems, n, total = audit(c, pats, rewrites)
        subj = git('log', '-1', '--format=%s', c)
        flag = 'BAD ' if problems else 'ok  '
        print(f'{flag}{c[:9]}  {n:4d} files {total / 1048576:6.1f} MB  {subj}')
        for p in problems:
            print(f'         {p}')
        bad += bool(problems)
    sys.exit(1 if bad else 0)


def force_reset(ms):
    head = git('symbolic-ref', '-q', 'HEAD', check=False)
    if head == f'refs/heads/{BRANCH}':
        die(f'{BRANCH} is checked out here — switch branches first')
    for _sha, tag, _msg in ms:
        if tag_commit(tag):
            git('tag', '-d', tag)
    if branch_tip():
        git('update-ref', '-d', f'refs/heads/{BRANCH}')
    print(f'--force: deleted {BRANCH} and its milestone tags. If they were '
          f'already pushed, the next push must be a force-push and GitHub '
          f'history moves under anyone who cloned it.')


def main(argv):
    force = '--force' in argv
    check = '--check' in argv
    append = None
    if '--append' in argv:
        i = argv.index('--append')
        if len(argv) < i + 4:
            die('--append needs REV TAG MESSAGE...')
        # one line, single spaces: what gets built must equal what gets recorded
        append = (argv[i + 1], argv[i + 2], ' '.join(' '.join(argv[i + 3:]).split()))
    recut = None
    if '--recut' in argv:
        i = argv.index('--recut')
        if len(argv) < i + 3:
            die('--recut needs REV TAG [MESSAGE...]')
        recut = (argv[i + 1], argv[i + 2], ' '.join(' '.join(argv[i + 3:]).split()))
    if append and recut:
        die('--append and --recut are exclusive')

    known = {'--force', '--check', '--append', '--recut', '--no-rewrites'}
    tail = [argv.index(o) for o in ('--append', '--recut') if o in argv]
    for a in (argv[:min(tail)] if tail else argv):
        if a.startswith('--') and a not in known:
            die(f'unknown option {a!r} (--append / --recut REV TAG MESSAGE... goes last)')
    if not check and git('symbolic-ref', '-q', 'HEAD', check=False) == f'refs/heads/{BRANCH}':
        die(f'{BRANCH} is checked out here — switch to main first')

    ms = parse_milestones()
    rules = parse_excludes()
    rewrites = parse_rewrites(allow_missing='--no-rewrites' in argv)
    tags = [t for _, t, _ in ms]
    for i in range(len(ms) + 1):        # a bad rule file dies here, before
        rules_for(i, tags, rules)       # --recut / --force drop anything
    print(f'rules: {len(rules)} exclude, {len(rewrites)} rewrite', file=sys.stderr)

    if check:
        check_only(rules, rewrites, tags)

    license_blob = git('rev-parse', '-q', '--verify', 'HEAD:LICENSE', check=False)
    if not license_blob:
        die('LICENSE must be committed on the current branch first')

    if append:
        rev, tag, msg = append
        if (not msg or tag.startswith('-') or tag.split() != [tag]
                or subprocess.run(['git', 'check-ref-format', f'refs/tags/{tag}'],
                                  cwd=ROOT, capture_output=True).returncode):
            die(f'--append: {tag!r} is not a valid tag name, or MESSAGE is empty')
        if tag in tags or tag_commit(tag):
            die(f'tag {tag} already exists')
        src = git('rev-parse', '-q', '--verify', f'{rev}^{{commit}}', check=False)
        if not src:
            die(f'--append: {rev!r} is not a commit')
        ms.append((src, tag, msg))
        tags.append(tag)
        # its milestones.txt line is written at the end, once the snapshot exists

    if recut:
        rev, tag, msg = recut
        if not ms or ms[-1][1] != tag:
            die(f'--recut: {tag} is not the last milestone'
                + (f' ({ms[-1][1]} is) — only the last one can be redone' if ms else ''))
        old_sha, _, old_msg = ms[-1]
        existing = tag_commit(tag)
        published = git('rev-parse', '-q', '--verify',
                        f'refs/remotes/{REMOTE}/{RBRANCH}^{{commit}}', check=False)
        if existing and published and is_ancestor(existing, published):
            die(f'--recut: {tag} is already on {REMOTE}/{RBRANCH} (as of your last '
                f'fetch) — published history must not move; --append the next tag instead')
        src = git('rev-parse', '-q', '--verify', f'{rev}^{{commit}}', check=False)
        if not src:
            die(f'--recut: {rev!r} is not a commit')
        if src == old_sha and not msg:
            die(f'--recut: {tag} is already cut from {src[:9]} with that message')
        prev = tag_commit(ms[-2][1]) if len(ms) > 1 else ''
        tip = branch_tip()
        if existing:
            if tip and tip != existing and is_ancestor(existing, tip):
                die(f'--recut: {BRANCH} has commits above {tag} (pulled from GitHub?) '
                    f'— it cannot be redone')
            if tip == existing:
                advance(tip, prev)          # back to the previous milestone
            git('tag', '-d', tag)
            print(f'--recut: dropped {tag} ({existing[:9]}); {BRANCH} -> '
                  f'{prev[:9] if prev else "(none)"}', file=sys.stderr)
        ms[-1] = (src, tag, msg or old_msg)

    if force:
        force_reset(ms)

    tip = branch_tip()
    last = ''                       # the previous milestone's commit
    built = 0
    print(f'{"tag":6} {"commit":9}  {"date":10}  {"files":>5} {"MB":>6}  {"-excl":>5} {"~rw":>3}  status')
    for i, (sha, tag, msg) in enumerate(ms):
        pats = rules_for(i, tags, rules)
        existing = tag_commit(tag)
        if existing:
            if not (tip and is_ancestor(existing, tip)):
                # Tag exists but the branch does not reach it. Adopt it when it
                # fast-forwards the chain (a run that died between tagging and
                # moving the branch leaves exactly this); anything else is stale.
                if (is_ancestor(tip, existing) if tip else
                        is_ancestor(last, existing) if last else not parents(existing)):
                    advance(tip, existing)
                    tip = existing
                else:
                    die(f'tag {tag} ({existing[:9]}) is not on {BRANCH} and does not '
                        f'continue it — was the branch reset? Use --force to rebuild '
                        f'everything.')
            problems, n, total = audit(existing, pats, rewrites)
            date = git('log', '-1', '--format=%aI', existing)
            status = 'exists' + (f' — WARN: {"; ".join(problems)}' if problems else '')
            print(f'{tag:6} {existing[:9]}  {date[:10]}  {n:5d} {total / 1048576:6.1f}  {"":>5} {"":>3}  {status}')
            last = existing
            continue
        src = git('rev-parse', '--verify', f'{sha}^{{commit}}')
        date = git('log', '-1', '--format=%aI', src)
        tree, removed, rewritten = snapshot_tree(src, pats, rewrites, license_blob)
        commit = make_commit(tree, tip, tag, msg, date)
        problems, n, total = audit(commit, pats, rewrites)
        if problems:
            die(f'{tag}: snapshot fails its own audit: ' + '; '.join(problems))
        git('tag', '-a', tag, commit, '-m', f'{tag}: {msg}', env=identity_env(date))
        advance(tip, commit)        # the branch moves with every tag: no window
        tip = last = commit         # in which a tag exists off-branch
        built += 1
        print(f'{tag:6} {commit[:9]}  {date[:10]}  {n:5d} {total / 1048576:6.1f}  '
              f'{len(removed):5d} {len(rewritten):3d}  built from {src[:9]}')

    print(f'{BRANCH} -> {tip[:9]}  ({built} new snapshot{"s" if built != 1 else ""}; '
          f'nothing pushed)')
    if append:
        src, tag, msg = ms[-1]
        with MILESTONES.open('a') as f:
            f.write(f'{src} {tag} {msg}\n')
        print(f'recorded {tag} = {src[:9]} in {MILESTONES.relative_to(ROOT)} (commit it)')
    if recut:
        src, tag, msg = ms[-1]
        lines = MILESTONES.read_text().splitlines(keepends=True)
        hits = [k for k, ln in enumerate(lines) if ln.split()[:2] == [old_sha, tag]]
        if len(hits) != 1:
            die(f'--recut: could not find the {tag} line in milestones.txt to update')
        lines[hits[0]] = f'{src} {tag} {msg}\n'
        MILESTONES.write_text(''.join(lines))
        print(f'recorded {tag} = {src[:9]} in {MILESTONES.relative_to(ROOT)} '
              f'(was {old_sha[:9]}; commit it)')


if __name__ == '__main__':
    main(sys.argv[1:])
