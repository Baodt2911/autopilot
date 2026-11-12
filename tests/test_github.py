import json
from pathlib import Path

import pytest

from autopilot.config import Config
from autopilot.github import AuthError, CmdError, GitHub

FIX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FIX / name).read_text())


def cfg():
    return Config(name="t", repo=Path("/r"), author="author-x", gh_user="vyn-bao-do",
                  limits={}, notify={}, reply={}, pr_rules={}, tests={}, worker={})


class Fake:
    """Fake runner: routes by argv/variables, records (argv, env)."""

    def __init__(self, search=None, node=None, repository=None, token="tok-vyn"):
        self.calls, self.search, self.node = [], search, node or {}
        self.repository, self.token = repository, token

    def __call__(self, args, env):
        self.calls.append((args, env))
        if args[:3] == ["gh", "auth", "token"]:
            if self.token is None:
                raise CmdError("no token")
            return self.token + "\n"
        if args[:2] == ["git", "-C"]:
            return "git@github.com:example/repo.git\n"
        v = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("-")}
        q = v.get("query", "")
        if "search(" in q:
            return json.dumps(self.search(v))
        if "node(id" in q:
            return json.dumps(self.node[(v["id"], v.get("c"))])
        if "pullRequest(number" in q:
            return json.dumps(self.repository)
        if args[:3] == ["gh", "api", "repos/example/repo/issues/7/comments"]:
            return json.dumps({"html_url": "https://github.com/example/repo/pull/7#issuecomment-9"})
        raise AssertionError(args)


def full_fake():
    return Fake(
        search=lambda v: fx("pr_page1.json"),
        node={("PR_101", "T100"): fx("pr_page2.json"), ("T_001", "C1"): fx("pr_comments_page2.json")},
        repository=fx("pr_merged.json"),
    )


def test_uses_token_of_gh_user_for_every_call(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-wrong")
    f = full_fake()
    gh = GitHub(cfg(), f)
    gh.open_prs()
    gh.recently_closed([102])
    gh.post_comment(7, "hi")
    assert f.calls[0][0] == ["gh", "auth", "token", "--user", "vyn-bao-do"]
    api = [c for c in f.calls if c[0][:2] == ["gh", "api"]]
    assert len(api) >= 4
    assert all(env["GH_TOKEN"] == "tok-vyn" for _, env in api)
    assert sum(1 for c in f.calls if c[0][:3] == ["gh", "auth", "token"]) == 1
    assert all(isinstance(a, str) for c, _ in f.calls for a in c)


@pytest.mark.parametrize("token", [None, "  "])
def test_auth_failure_raises(token):
    with pytest.raises(AuthError):
        GitHub(cfg(), Fake(token=token)).token()
    with pytest.raises(AuthError):
        GitHub(cfg(), Fake(token=token)).open_prs()


def test_other_gh_failure_is_cmd_error_not_auth():
    def boom(v):
        raise CmdError("HTTP 502")
    with pytest.raises(CmdError) as e:
        GitHub(cfg(), Fake(search=boom)).open_prs()
    assert not isinstance(e.value, AuthError)


def test_parses_snapshot_fields():
    (pr,) = GitHub(cfg(), full_fake()).open_prs()
    assert (pr.number, pr.is_draft, pr.state) == (101, False, "OPEN")
    assert (pr.head_ref, pr.head_sha) == ("feat/x-101", "a" * 40)
    assert (pr.base_ref, pr.base_sha) == ("develop", "b" * 40)
    assert pr.mergeable == "CONFLICTING" and pr.review_decision == "CHANGES_REQUESTED"
    assert pr.failed_checks == ("unit-tests", "lint", "e2e")
    assert pr.changed_files == ("src/a.py", "src/b.py")
    assert pr.reviews[0].author == "reviewer-a" and pr.reviews[0].state == "CHANGES_REQUESTED"
    assert [c.id for c in pr.comments] == ["IC_1", "IC_2"]
    c = pr.threads[0].comments[0]
    assert (c.thread_id, c.path, c.line) == ("T_001", "src/a.py", 10)
    assert c.created_at.year == 2026 and c.created_at.tzinfo is not None


def test_search_query_and_draft_included():
    f = full_fake()
    GitHub(cfg(), f).open_prs()
    sq = [a for a in next(c for c, _ in f.calls if any("search(" in x for x in c))]
    assert "q=is:pr is:open author:author-x repo:example/repo" in sq


def test_paginates_threads_and_comments():
    f = full_fake()
    (pr,) = GitHub(cfg(), f).open_prs()
    assert len(pr.threads) == 105  # 100 on page 1 + 5 on page 2
    assert [t.id for t in pr.threads][-1] == "T_105"
    assert len({t.id for t in pr.threads}) == 105
    assert [c.id for c in pr.threads[0].comments] == ["RC_1", "RC_2", "RC_3"]  # 2 pages
    cursors = [{a.split("=")[0]: a.split("=")[1] for a in c if "=" in a and not a.startswith("-")}
               for c, _ in f.calls if any("node(id" in x for x in c)]
    assert {(v["id"], v["c"]) for v in cursors} == {("PR_101", "T100"), ("T_001", "C1")}


def test_paginates_pr_search():
    p1, p2 = fx("pr_page1.json"), fx("pr_page1.json")
    p1["data"]["search"]["pageInfo"] = {"hasNextPage": True, "endCursor": "S1"}
    p2["data"]["search"]["nodes"][0]["number"] = 103
    seen = []

    def search(v):
        seen.append(v.get("cursor"))
        return p2 if v.get("cursor") == "S1" else p1
    f = full_fake()
    f.search = search
    prs = GitHub(cfg(), f).open_prs()
    assert [p.number for p in prs] == [101, 103] and seen == [None, "S1"]


def test_bot_flag_from_typename():
    (pr,) = GitHub(cfg(), full_fake()).open_prs()
    assert [c.is_bot for c in pr.comments] == [False, True]
    assert pr.threads[0].comments[2].is_bot is True
    assert pr.threads[0].comments[0].is_bot is False


def test_recently_closed_merged():
    f = full_fake()
    (pr,) = GitHub(cfg(), f).recently_closed([102])
    assert pr.state == "MERGED" and pr.merged_at.isoformat().startswith("2026-01-03T05:00:00")
    assert GitHub(cfg(), f).recently_closed([]) == []


def test_post_comment_returns_url_and_body_is_single_arg():
    f = full_fake()
    body = "x; rm -rf / $(whoami) `id`"
    url = GitHub(cfg(), f).post_comment(7, body)
    assert url.endswith("#issuecomment-9")
    args = f.calls[-1][0]
    assert f"body={body}" in args
    assert GitHub(cfg(), f).post_issue_comment(7, "b").endswith("#issuecomment-9")
