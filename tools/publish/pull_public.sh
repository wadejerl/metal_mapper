#!/bin/bash
# pull_public.sh — bring commits made on GitHub (web edits, merged PRs) back
# into the journal. Dry run by default; --apply cherry-picks them onto main
# and fast-forwards the local `public` branch to what GitHub has.
#
#   tools/publish/pull_public.sh            # show what is new on GitHub
#   tools/publish/pull_public.sh --apply    # cherry-pick onto main, advance public
#
# Walks GitHub's first-parent line, so a merged pull request arrives as ONE
# cherry-pick of its merge commit (-m 1): the PR's net change. `public` is
# advanced after every pick, so a run that stops on a conflict resumes where
# it stopped once that pick is finished by hand (it prints how).
set -euo pipefail
REMOTE="${MM_PUBLIC_REMOTE:-github}"
BRANCH="${MM_PUBLIC_BRANCH:-public}"
RBRANCH="${MM_PUBLIC_REMOTE_BRANCH:-main}"

git fetch "$REMOTE"
git rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null \
    || { echo "error: no local branch '$BRANCH' — build_public.py creates it" >&2; exit 1; }
gitdir="$(git rev-parse --git-dir)"
if [ -e "$gitdir/sequencer" ] || [ -e "$gitdir/CHERRY_PICK_HEAD" ]; then
    echo "error: a cherry-pick is in progress — finish it (fix, git add, git cherry-pick" \
         "--continue, then git branch -f $BRANCH <that commit>) or drop it" \
         "(git cherry-pick --abort), then re-run" >&2
    exit 1
fi
if ! git merge-base --is-ancestor "$BRANCH" "$REMOTE/$RBRANCH"; then
    if git merge-base --is-ancestor "$REMOTE/$RBRANCH" "$BRANCH"; then
        echo "nothing new on $REMOTE/$RBRANCH; local $BRANCH is AHEAD of it — unpushed snapshot(s):"
        git log --oneline "$REMOTE/$RBRANCH..$BRANCH"
        echo "push them: git push --no-verify --atomic $REMOTE $BRANCH:$RBRANCH --follow-tags"
        exit 0
    fi
    # Diverged: GitHub moved AND local public has commits GitHub lacks. That is
    # what a rejected push looks like. If the local extras are all tagged
    # snapshots, they can be undone and redone on top of the pull.
    base="$(git merge-base "$BRANCH" "$REMOTE/$RBRANCH")"
    untagged=0; recipe=""
    for c in $(git rev-list --reverse "$base..$BRANCH"); do
        t="$(git tag --points-at "$c" | head -1)"
        [ -n "$t" ] || untagged=1
        recipe="$recipe    git tag -d ${t:-<no tag: ${c:0:9}>}      # and drop its line from tools/publish/milestones.txt"$'\n'
    done
    if [ "$untagged" = 0 ]; then
        cat >&2 <<MSG
error: $REMOTE/$RBRANCH moved since local $BRANCH was built on it (a push of these
would be rejected):
$(git log --oneline "$base..$BRANCH")
Undo the unpushed snapshot(s), pull, then redo them on top:
$recipe    git branch -f $BRANCH ${base:0:9}
    tools/publish/pull_public.sh --apply
    tools/publish/build_public.py --append ...    # same REV/TAG/MESSAGE as before
MSG
    else
        echo "error: $REMOTE/$RBRANCH and local $BRANCH have diverged and the local extras" \
             "are not all tagged snapshots — GitHub's history was rewritten, or $BRANCH" \
             "was moved by hand. Sort that out by hand." >&2
    fi
    exit 1
fi
new="$(git rev-list --reverse --first-parent "$BRANCH..$REMOTE/$RBRANCH")"
if [ -z "$new" ]; then
    echo "nothing new on $REMOTE/$RBRANCH"
    exit 0
fi
echo "new on $REMOTE/$RBRANCH (a merged PR counts once, as its merge commit):"
git log --oneline --first-parent "$BRANCH..$REMOTE/$RBRANCH"
if [ "${1:-}" != "--apply" ]; then
    echo
    echo "dry run — re-run with --apply to cherry-pick these onto main and advance $BRANCH"
    exit 0
fi
[ "$(git symbolic-ref --short HEAD)" = "main" ] \
    || { echo "error: check out main first" >&2; exit 1; }
git diff --quiet && git diff --cached --quiet \
    || { echo "error: uncommitted changes — commit or stash first" >&2; exit 1; }
for c in $new; do
    # -x records "(cherry picked from commit …)" so the journal shows the origin;
    # -m 1 turns a PR merge commit into its net change (plain commits: no -m)
    m=()
    [ "$(git rev-list --parents -n1 "$c" | wc -w)" -gt 2 ] && m=(-m 1)
    if ! git cherry-pick -x ${m[@]+"${m[@]}"} "$c"; then
        cat >&2 <<MSG

error: cherry-pick of ${c:0:9} stopped (see git's message above). $BRANCH is at
the last commit that did apply. To finish:
    fix the files, git add <them>, git cherry-pick --continue
    git branch -f $BRANCH $c
    tools/publish/pull_public.sh --apply      # picks the rest
Or give this one up: git cherry-pick --abort (main is back where it was).
MSG
        exit 1
    fi
    git branch -f "$BRANCH" "$c"
done
echo "done: main has the new commits; $BRANCH now at $REMOTE/$RBRANCH."
echo "The next build_public.py snapshot includes them without duplication."
