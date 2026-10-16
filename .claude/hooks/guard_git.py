#!/usr/bin/env python3
"""PreToolUse 훅 — 커밋 안 된 작업을 지우거나 원격을 덮어쓰는 git 명령을 막는다.

2026-10-14: 커밋 메시지를 고치던 `sed` 줄 끝에 「확인용」으로 붙은
`git -C <워크트리> reset --hard <sha>`가 실제로 돌아, 검증까지 끝난 미커밋 수정 12개 파일
(+888/−24)이 사라졌다(에이전트 기록을 다시 적용해 복구). 명령 체인 어디에 있든,
`-C <경로>` 같은 앞 옵션이 붙든 잡는다. 정말 필요하면 사람이 프롬프트에서 `! <명령>`으로
직접 돌린다 — 에이전트의 Bash 도구만 막는다.

종료 코드 2 + stderr = 차단(이유가 에이전트에게 간다). 0 = 통과.
"""

from __future__ import annotations

import json
import re
import sys

_PATH = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_GIT = (
    r"(?:^|[\s;&|(`$!])(?:\S*/)?git"
    r"(?:\s+(?:-C\s+" + _PATH + r"|-c\s+\S+|--(?:git-dir|work-tree|namespace)(?:=|\s+)"
    + _PATH + r"|--no-pager|--bare|-P|--no-optional-locks))*\s+"
)
_REST = r"[^;&|\n]*"
_RULES: tuple[tuple[str, str], ...] = (
    (r"reset\b" + _REST + r"\s--(?:hard|merge)\b", "reset --hard/--merge는 미커밋 수정을 지운다"),
    (
        r"checkout\b" + _REST + r"(?:\s-f\b|\s--force\b|\s--(?:\s|$)|\s\.(?:\s|$))",
        "checkout -f / -- <경로> / . 는 작업 트리 수정을 버린다",
    ),
    (r"restore\b(?!" + _REST + r"--staged)", "restore(--staged 없이)는 작업 트리 수정을 버린다"),
    (r"clean\b" + _REST + r"\s-[a-zA-Z]*f", "clean -f는 추적 안 된 파일을 지운다"),
    (r"stash\s+(?:drop|clear)\b", "stash drop/clear는 보관한 작업을 지운다"),
    (
        r"push\b" + _REST + r"(?:\s-f\b|\s--force\b|\s--force-with-lease\b|\s\+\S)",
        "강제 push는 원격 기록을 덮어쓴다",
    ),
    (r"worktree\s+remove\b" + _REST + r"\s(?:-f\b|--force\b)", "worktree remove --force는 미커밋 작업을 지운다"),
)
_COMPILED = tuple((re.compile(_GIT + pattern), why) for pattern, why in _RULES)

_ADVICE = (
    "이 명령은 이 저장소의 훅이 막았습니다(.claude/hooks/guard_git.py). "
    "확인용 줄로 파괴적인 git 명령을 쓰지 마세요. 대안: 워크트리 기준 맞추기는 깨끗한 트리에서 "
    "`git checkout -B <브랜치> <sha>`(수정을 지우지 않는다 — 겹치면 git이 거절한다), 작업 보관은 WIP 커밋 또는 "
    "`git stash push -u -m <태그>`, 기준 확인은 `git rev-parse HEAD`·`git status --short`. "
    "정말 필요하면 사람에게 프롬프트에서 `! <명령>`으로 직접 실행해 달라고 요청하세요."
)


def violation(command: str) -> str | None:
    for pattern, why in _COMPILED:
        if pattern.search(command):
            return why
    return None


def _self_test() -> int:
    blocked = [
        "git reset --hard HEAD",
        "sed -i '' s/a/b/ f; W=/x\ngit -C $W reset --hard -q a4b4c1c 2>/dev/null && echo WRONG || true",
        'git -C "/a b/c" reset --hard',
        "cd x && /usr/bin/git reset --merge",
        "git checkout -- src/a.py",
        "git checkout .",
        "git checkout -f main",
        "git restore src/a.py",
        "git clean -fd",
        "git clean -xdf",
        "git stash drop stash@{0}",
        "git stash clear",
        "git push --force origin main",
        "git push -f",
        "git push origin +main",
        "git push --force-with-lease",
        "git worktree remove --force .claude/worktrees/x",
        "git worktree remove -f x",
        "git --no-pager -c core.x=y reset --hard",
    ]
    allowed = [
        "git reset --soft HEAD~1",
        "git reset HEAD file",
        "git restore --staged a.py",
        "git checkout -b feature",
        "git checkout -B feature abc123",
        "git switch main",
        "git clean -n",
        "git stash push -u -m tag",
        "git push origin main",
        "git worktree remove .claude/worktrees/x",
        "git status --short",
        "grep -n 'reset --hard' notes.md",
        "echo done",
        "git log --oneline -3",
    ]
    bad = [c for c in blocked if violation(c) is None] + [c for c in allowed if violation(c)]
    for command in bad:
        sys.stderr.write(f"FAIL: {command!r}\n")
    sys.stdout.write(f"{len(blocked) + len(allowed) - len(bad)}/{len(blocked) + len(allowed)} ok\n")
    return 1 if bad else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return _self_test()
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # 훅 입력을 못 읽으면 막지 않는다 — 판단할 명령이 없다
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    why = violation(command)
    if why is None:
        return 0
    sys.stderr.write(f"차단: {why}. {_ADVICE}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
