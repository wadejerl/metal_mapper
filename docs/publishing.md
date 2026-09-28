# Publishing to GitHub: the `public` branch

The development journal (`main`, which lives on a private server) never goes
to GitHub as history. What GitHub gets is the `public` branch: a short, straight
line of **snapshot commits**, one per milestone, each carrying the complete
tree of `main` at that point minus a few excluded paths, with `rewrite.txt`'s substitutions applied,
plus `LICENSE`.
The tooling lives in `tools/publish/`:

| File | Role |
|---|---|
| `milestones.txt` | the chain: `<journal commit> <tag> <message>`, oldest first |
| `exclude.txt` | paths dropped from every snapshot (ST's RM0440 PDF, build output, the retired firmware trees, the SLA0044 USB Host library) |
| `rewrite.txt` | byte substitutions in matching files (the pre-v0.8 units carried one box's account name; public history gets `pi`) |
| `build_public.py` | builds missing snapshots onto `public`, tags them, audits every one |
| `pull_public.sh` | brings commits made on GitHub back into `main` |

`build_public.py` is git plumbing on a private temporary index: it never
touches the real index or any other branch, and it never pushes. Its one
working-tree write is `--append` adding the new line to
`tools/publish/milestones.txt` (only once the snapshot exists), which you
then commit. A snapshot is a pure function of its inputs (fixed author, dates
copied from the journal commit), so a `--force` rebuild with unchanged inputs
reproduces byte-identical commits.

## One-time setup

1. Create the GitHub repository **empty** — no README, license or
   .gitignore initialised there, or the histories will not join.
2. Add it as a second remote and push the branch as GitHub's `main`, tags
   along. `--atomic` means every ref lands or none does; `--no-verify` skips
   a pre-push hook that restricts push targets (drop it if you have none):

```bash
git remote add github git@github.com:<you>/metal_mapper.git
```

```bash
git push --no-verify --atomic github public:main --follow-tags
```

## Publishing a milestone

1. Bring in whatever changed on GitHub first — `--append` builds on your
   local `public` and never looks at GitHub:

```bash
tools/publish/pull_public.sh            # then --apply if it lists anything
```

2. Snapshot `HEAD`, the last commit on `main` — so commit first; uncommitted
   edits are not in the snapshot:

```bash
tools/publish/build_public.py --append HEAD v0.9 "one line on what changed"
```

   That builds the snapshot, tags it, prints the audit, and records the line
   in `milestones.txt` — commit that (`git commit -am "publish: record v0.9"`).

3. Push:

```bash
git push --no-verify --atomic github public:main --follow-tags
```

**Redoing a milestone before it is pushed.** While you are still polishing
`v0.8` locally (or a snapshot was pushed nowhere yet), fix things on `main`,
commit, and re-cut the same tag from the new commit instead of adding a
`v0.9`:

```bash
tools/publish/build_public.py --recut HEAD v0.8            # message kept
```

It drops the tag, moves `public` back to the previous milestone, snapshots
again, and updates the tag's line in `milestones.txt` — commit that. Only the
last milestone can be redone, and the script refuses once that tag is on
`github/main` (as of your last fetch): published history never moves.

**If the push is rejected** as non-fast-forward, GitHub moved after step 1.
Because of `--atomic` nothing landed there, not even the tag. Run
`tools/publish/pull_public.sh` again: it sees your unpushed snapshot and
prints the undo steps (delete the tag, move `public` back, drop the line from
`milestones.txt`). Then redo steps 1–3.

## Pulling GitHub changes back

Edits made in GitHub's web editor or merged pull requests land on GitHub's
`main` ahead of your `public`. Bring them into the journal:

```bash
tools/publish/pull_public.sh
```

Dry run: lists what is new. `--apply` cherry-picks those commits onto `main`
(with `-x`, so the journal records where each came from) and fast-forwards
`public` to GitHub's tip. A merged pull request comes over as one cherry-pick
of its merge commit — the PR's net change. If a pick conflicts, the script
stops and prints how to finish that one by hand and resume. The next snapshot
then includes everything without duplication, because a snapshot is simply
the current state of `main`.

## Rules

- Never `git merge` or `git rebase` between `main` and `public`. They share
  no ancestor; Git would hand you a wall of "both added" conflicts.
- Never push `main` to GitHub.
- Never edit an existing line of `milestones.txt`. Existing tags are left
  alone (they may already be public) and only audited against the current
  rules — an excluded path present, a scrub string present, `LICENSE`
  missing, a blob over 50 MB. A rule that got looser is not detected.
- Run `build_public.py` from a `main` checkout. It refuses to run without
  `tools/publish/rewrite.txt` (kept out of `public` on purpose) unless told
  `--no-rewrites`, which turns the scrub and its audit off, and it refuses
  to move `public` while that branch is checked out.
- `--force` deletes `public` and every milestone tag and rebuilds. After the
  first push that means a force-push and moving history under anyone who
  cloned — reserve it for before the repository is public.
- `build_public.py --check` audits every commit on `public` against the
  current rules (excluded paths, LICENSE present, blobs over 50 MB).

## What the public tree does not have

- `docs/rm0440.pdf` — ST's reference manual is copyrighted and 37 MB.
- CubeIDE build output, per-machine `.launch` files, and the object files
  the detector1/2 projects once tracked.
- The 2026-07-26 checkpoint's root-level `Core/ Drivers/ EWARM/ Middlewares/`
  trees, `boot.log`, `.mxproject`, a copied `stm_detector3.ioc`, the
  `stm32f303_detector` and `stm_pi_detector` trees, and from `v0.3` the stale
  root `metal_mapper.ioc`.
- `Middlewares/ST/STM32_USB_Host_Library` from the H723 prototypes: ST's
  SLA0044 licence is not GPL-compatible.
- From `v0.3` on, the retired `stm_detector` / `stm_detector2` projects.
- The `.claude/` settings directories the H723 projects tracked.
- One box's account name in the pre-v0.8 systemd units (rewritten to
  `pi`), the bench site's coordinates in the demo generators and an early README
  example (rewritten to a Black Rock City point), and a comment's path to
  the RM0440 PDF.
- `tools/publish/rewrite.txt` itself, which names those strings; the public
  copy of `build_public.py` refuses to run without it unless told
  `--no-rewrites`.

The head-unit systemd units are templates rendered by `install_pi_system.sh`
(see the README's Pi Deployment section); the base Pi's `rtkbase/` units are
deployed by hand and assume an `rtk` account.
