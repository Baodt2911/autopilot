"""GitHub client: `gh api graphql` -> PRSnapshot, pinned to cfg.gh_user's token."""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

from .config import Config
from .models import Comment, PRSnapshot, Review, Thread

FAIL_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED"})
PAGE = 100  # continuation pages (single level, cheap)
# Inline page sizes: GitHub rejects queries whose requested node count (product of
# nested `first` values) exceeds 500k, so keep these small and drain the rest.
SEARCH_FIRST = 10
THREADS_FIRST = 25
THREAD_COMMENTS_FIRST = 20
LIST_FIRST = 50  # reviews, issue comments, files, check contexts


class CmdError(Exception):
    """A command (gh/git) exited non-zero or returned an unusable response."""


class AuthError(Exception):
    """No usable GitHub token for cfg.gh_user."""


def run_cmd(args: list[str], env: dict) -> str:
    p = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise CmdError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed "
                       f"(exit {p.returncode}): {p.stderr.strip()}")
    return p.stdout


_PI = "pageInfo { hasNextPage endCursor }"
_REVIEW_COMMENT = """id author { login __typename } body createdAt updatedAt url path line"""
_ISSUE_COMMENT = """id author { login __typename } body createdAt updatedAt url"""

_PR_FIELDS = f"""
  id number title url isDraft state mergedAt
  headRefName headRefOid baseRefName baseRefOid mergeable reviewDecision
  reviewThreads(first: {THREADS_FIRST}) {{
    nodes {{ id isResolved comments(first: {THREAD_COMMENTS_FIRST}) {{ nodes {{ {_REVIEW_COMMENT} }} {_PI} }} }}
    {_PI}
  }}
  reviews(first: {LIST_FIRST}) {{ nodes {{ id author {{ login }} state submittedAt body url }} {_PI} }}
  comments(first: {LIST_FIRST}) {{ nodes {{ {_ISSUE_COMMENT} }} {_PI} }}
  files(first: {LIST_FIRST}) {{ nodes {{ path }} {_PI} }}
  commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{
    contexts(first: {LIST_FIRST}) {{ nodes {{ __typename ... on CheckRun {{ name conclusion }} }} }}
  }} }} }} }}
"""

OPEN_PRS_QUERY = f"""
query($q: String!, $cursor: String) {{
  search(query: $q, type: ISSUE, first: {SEARCH_FIRST}, after: $cursor) {{
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} }} }}
    {_PI}
  }}
}}"""

PR_BY_NUMBER_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ pullRequest(number: $number) {{ {_PR_FIELDS} }} }}
}}"""

# Continuation pages, addressed by node id.
_THREADS_MORE = f"""
query($id: ID!, $c: String) {{ node(id: $id) {{ ... on PullRequest {{
  reviewThreads(first: {PAGE}, after: $c) {{
    nodes {{ id isResolved comments(first: {THREAD_COMMENTS_FIRST}) {{ nodes {{ {_REVIEW_COMMENT} }} {_PI} }} }} {_PI}
  }} }} }} }}"""
_THREAD_COMMENTS_MORE = f"""
query($id: ID!, $c: String) {{ node(id: $id) {{ ... on PullRequestReviewThread {{
  comments(first: {PAGE}, after: $c) {{ nodes {{ {_REVIEW_COMMENT} }} {_PI} }} }} }} }}"""
_REVIEWS_MORE = f"""
query($id: ID!, $c: String) {{ node(id: $id) {{ ... on PullRequest {{
  reviews(first: {PAGE}, after: $c) {{ nodes {{ id author {{ login }} state submittedAt body url }} {_PI} }}
  }} }} }}"""
_COMMENTS_MORE = f"""
query($id: ID!, $c: String) {{ node(id: $id) {{ ... on PullRequest {{
  comments(first: {PAGE}, after: $c) {{ nodes {{ {_ISSUE_COMMENT} }} {_PI} }} }} }} }}"""
_FILES_MORE = f"""
query($id: ID!, $c: String) {{ node(id: $id) {{ ... on PullRequest {{
  files(first: {PAGE}, after: $c) {{ nodes {{ path }} {_PI} }} }} }} }}"""


_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _login(author: dict | None) -> str:
    return (author or {}).get("login") or "ghost"


def _comment(n: dict, thread_id: str | None = None) -> Comment:
    a = n.get("author") or {}
    return Comment(
        id=n["id"], author=_login(a), is_bot=a.get("__typename") == "Bot",
        body=n.get("body") or "", created_at=_ts(n["createdAt"]), updated_at=_ts(n["updatedAt"]),
        url=n["url"], thread_id=thread_id, path=n.get("path"), line=n.get("line"),
    )


def _review(n: dict) -> Review:
    return Review(id=n["id"], author=_login(n.get("author")), state=n["state"],
                  submitted_at=_ts(n.get("submittedAt")) or _MIN_TS, body=n.get("body") or "",
                  url=n["url"])


def _parse_slug(url: str) -> str:
    u = url.strip()
    m = (re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", u)
         # scp-style SSH with a host alias from ~/.ssh/config, e.g. git@github-personal:owner/repo.git
         or re.fullmatch(r"[\w.-]+@[\w.-]+:([^/\s]+)/([^/\s]+?)(?:\.git)?/?", u))
    if not m:
        raise CmdError(f"cannot derive owner/repo from remote url: {url!r}")
    return f"{m.group(1)}/{m.group(2)}"


class GitHub:
    def __init__(self, cfg: Config, run: Callable[[list[str], dict], str] = run_cmd):
        self.cfg, self._run = cfg, run
        self._token: str | None = None
        self._slug: str | None = None

    # -- plumbing ---------------------------------------------------------
    def token(self) -> str:
        if self._token is None:
            try:
                out = self._run(["gh", "auth", "token", "--user", self.cfg.gh_user], dict(os.environ))
            except CmdError as e:
                raise AuthError(f"no gh token for user {self.cfg.gh_user}: {e}") from e
            tok = out.strip()
            if not tok:
                raise AuthError(f"empty gh token for user {self.cfg.gh_user}")
            self._token = tok
        return self._token

    def _env(self) -> dict:
        return {**os.environ, "GH_TOKEN": self.token()}

    def slug(self) -> str:
        if self._slug is None:
            out = self._run(["git", "-C", str(self.cfg.repo), "remote", "get-url", "origin"],
                            dict(os.environ))
            self._slug = _parse_slug(out)
        return self._slug

    def _graphql(self, query: str, **variables: str | int | None) -> dict:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for k, v in variables.items():
            if v is None:
                continue
            args += ["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"]
        out = self._run(args, self._env())
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise CmdError(f"gh returned invalid JSON: {e}") from e
        if data.get("errors") or data.get("data") is None:
            raise CmdError(f"graphql error: {json.dumps(data.get('errors'))[:500]}")
        return data["data"]

    def _rest_post_comment(self, number: int, body: str) -> str:
        out = self._run(["gh", "api", f"repos/{self.slug()}/issues/{number}/comments",
                         "-X", "POST", "-f", f"body={body}"], self._env())
        try:
            return json.loads(out)["html_url"]
        except (json.JSONDecodeError, KeyError) as e:
            raise CmdError(f"unexpected comment response: {e}") from e

    # -- pagination -------------------------------------------------------
    def _drain(self, node_id: str, query: str, field: str, conn: dict) -> list[dict]:
        """All nodes of a connection: the inline first page plus continuation pages."""
        nodes = list(conn["nodes"])
        pi = conn["pageInfo"]
        while pi["hasNextPage"]:
            page = self._graphql(query, id=node_id, c=pi["endCursor"])["node"][field]
            nodes += page["nodes"]
            pi = page["pageInfo"]
        return nodes

    def _snapshot(self, n: dict) -> PRSnapshot:
        pid = n["id"]
        threads = []
        for t in self._drain(pid, _THREADS_MORE, "reviewThreads", n["reviewThreads"]):
            cs = self._drain(t["id"], _THREAD_COMMENTS_MORE, "comments", t["comments"])
            threads.append(Thread(id=t["id"], is_resolved=t["isResolved"],
                                  comments=tuple(_comment(c, t["id"]) for c in cs)))
        reviews = self._drain(pid, _REVIEWS_MORE, "reviews", n["reviews"])
        comments = self._drain(pid, _COMMENTS_MORE, "comments", n["comments"])
        files = self._drain(pid, _FILES_MORE, "files", n["files"])
        failed: list[str] = []
        for c in n["commits"]["nodes"]:
            rollup = (c.get("commit") or {}).get("statusCheckRollup") or {}
            for ctx in rollup.get("contexts", {}).get("nodes", []):
                if ctx.get("conclusion") in FAIL_CONCLUSIONS:
                    failed.append(ctx["name"])
        return PRSnapshot(
            number=n["number"], title=n["title"], url=n["url"], is_draft=n["isDraft"],
            state=n["state"], head_ref=n["headRefName"], head_sha=n["headRefOid"],
            base_ref=n["baseRefName"], base_sha=n["baseRefOid"], mergeable=n["mergeable"],
            review_decision=n.get("reviewDecision"), threads=tuple(threads),
            reviews=tuple(_review(r) for r in reviews),
            comments=tuple(_comment(c) for c in comments),
            failed_checks=tuple(failed), changed_files=tuple(f["path"] for f in files),
            merged_at=_ts(n.get("mergedAt")),
        )

    # -- public API -------------------------------------------------------
    def open_prs(self) -> list[PRSnapshot]:
        self.token()
        q = f"is:pr is:open author:{self.cfg.author} repo:{self.slug()}"
        out: list[PRSnapshot] = []
        cursor = None
        while True:
            s = self._graphql(OPEN_PRS_QUERY, q=q, cursor=cursor)["search"]
            out += [self._snapshot(n) for n in s["nodes"] if n]
            if not s["pageInfo"]["hasNextPage"]:
                return out
            cursor = s["pageInfo"]["endCursor"]

    def recently_closed(self, numbers: list[int]) -> list[PRSnapshot]:
        owner, name = self.slug().split("/", 1)
        out = []
        for num in numbers:
            pr = self._graphql(PR_BY_NUMBER_QUERY, owner=owner, name=name, number=num)
            n = pr["repository"]["pullRequest"]
            if n:
                out.append(self._snapshot(n))
        return out

    def pr_by_number(self, number: int) -> PRSnapshot | None:
        """Read-only fetch of one PR in any state (open, merged, closed)."""
        owner, name = self.slug().split("/", 1)
        n = self._graphql(PR_BY_NUMBER_QUERY, owner=owner, name=name, number=number)["repository"]["pullRequest"]
        return self._snapshot(n) if n else None

    def reply_to_comment(self, pr: int, comment_id: int, body: str) -> str:
        """Reply inside a review thread by the database id of one of its comments."""
        out = self._run(["gh", "api", f"repos/{self.slug()}/pulls/{pr}/comments/{comment_id}/replies",
                         "-X", "POST", "-f", f"body={body}"], self._env())
        try:
            return json.loads(out)["html_url"]
        except (json.JSONDecodeError, KeyError) as e:
            raise CmdError(f"unexpected reply response: {e}") from e

    def reply_to_thread(self, thread_id: str, body: str) -> str:
        q = ("mutation($t: ID!, $b: String!) { addPullRequestReviewThreadReply(input: "
             "{pullRequestReviewThreadId: $t, body: $b}) { comment { url } } }")
        d = self._graphql(q, t=thread_id, b=body)
        try:
            return d["addPullRequestReviewThreadReply"]["comment"]["url"]
        except (KeyError, TypeError) as e:
            raise CmdError(f"unexpected reply response: {e}") from e

    def post_comment(self, pr: int, body: str) -> str:
        return self._rest_post_comment(pr, body)

    def post_issue_comment(self, issue: int, body: str) -> str:
        return self._rest_post_comment(issue, body)
