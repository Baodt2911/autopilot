"""`autopilot web --demo`: fake project in a temp dir (own DB + config), no gh/git/claude needed."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..config import load_config
from ..models import WorkerStatus, Worktree
from ..store import Store

NAME = "demo"

Q_BODY = ('Reviewer hỏi: "Admin có được xóa đơn hàng đã giao không?"\n'
          "Spec: docs/orders.md:42 nói \"đơn đã giao chỉ được hủy bởi owner\"\n"
          "Vì sao không tự quyết: spec mâu thuẫn với use case UC-7\n"
          "A: Chặn xóa, chỉ cho hủy (đề xuất · ít ảnh hưởng)\n"
          "B: Cho admin xóa mềm (cần thêm cột deleted_at)")
P_BODY = ("Plan B cho PR #412: tách `OrderService` thành hai lớp.\n"
          "1. Chuyển logic tính giá sang `PricingService`.\n2. Giữ API cũ làm wrapper.\n"
          "3. Thêm test cho các nhánh giảm giá.\nChi tiết: .autopilot/plans/412-b.md")
R_BODY = ("Thanks for the catch. The null check is already handled in `parse_order()` "
          "(see line 88); I added a regression test so it stays covered.")


class DemoTransport:
    STATES = {"feat/orders-cancel": ("working", "9f3c1a72-aaaa-bbbb-cccc-000000000001"),
              "feat/pricing-split": ("waiting", "5be0d4c8-aaaa-bbbb-cccc-000000000002"),
              "fix/null-guard": ("idle", "c1d2e3f4-aaaa-bbbb-cccc-000000000003"),
              "feat/export-csv": ("limited", "77aa88bb-aaaa-bbbb-cccc-000000000004"),
              "chore/deps": ("owner_busy", "0a1b2c3d-aaaa-bbbb-cccc-000000000005"),
              "fix/flaky-test": ("none", None)}

    def set_worktrees(self, paths):
        pass

    def status(self, wt, now=None):
        st, sess = self.STATES.get(wt.branch, ("none", None))
        return WorkerStatus(st, sess, None)


def build_demo(root: Path | None = None):
    """Create the demo project under `root` (default: new temp dir).
    Returns (config_dir, transport_factory, worktree_lister, root)."""
    root = Path(root or tempfile.mkdtemp(prefix="autopilot-demo-"))
    cd, repo = root / "config", root / "repo"
    cd.mkdir(parents=True, exist_ok=True)
    repo.mkdir(parents=True, exist_ok=True)
    (cd / f"{NAME}.toml").write_text(f'repo="{repo}"\nauthor="demo-dev"\ngh_user="demo-dev"\n', encoding="utf-8")
    cfg = load_config(NAME, cd)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path, prefix=NAME)
    now = datetime.now().astimezone()

    def snap(n, title, head, base="main", draft=False, review=None, mergeable="MERGEABLE", failed=()):
        return {"number": n, "title": title, "url": f"https://github.com/acme/shop/pull/{n}",
                "is_draft": draft, "state": "OPEN", "head_ref": head, "head_sha": "a1", "base_ref": base,
                "base_sha": "b1", "mergeable": mergeable, "review_decision": review, "threads": [],
                "reviews": [], "comments": [], "failed_checks": list(failed), "changed_files": [],
                "merged_at": None}

    for s in (snap(412, "Tách OrderService", "feat/pricing-split", review="CHANGES_REQUESTED"),
              snap(413, "Hủy đơn đã giao", "feat/orders-cancel", base="feat/pricing-split", mergeable="CONFLICTING",
                   failed=("unit-tests",)),
              snap(415, "Guard null khi parse đơn", "fix/null-guard", review="APPROVED"),
              snap(418, "Xuất CSV", "feat/export-csv", draft=True),
              snap(420, "Cập nhật phụ thuộc", "chore/deps", review="REVIEW_REQUIRED")):
        store.save_snapshot(s["number"], s)
    store.add_item("Q", 413, 1, "https://github.com/acme/shop/pull/413#discussion_r1", Q_BODY, now - timedelta(hours=2))
    store.add_item("P", 412, 2, "Plan B: tách OrderService", P_BODY, now - timedelta(hours=1))
    store.add_item("R", 415, 3, "Nháp reply cho thread null-guard", R_BODY, now - timedelta(minutes=20))
    for pr, kind, text, ago in (
            (413, "comment", "Reviewer comment mới về quyền admin", 130), (413, "delivered", "Giao lô #1 cho worker", 125),
            (412, "noop", "Bỏ qua comment cảm ơn của reviewer", 90), (412, "escalated", "Plan B chờ owner duyệt", 60),
            (415, "review", "Review APPROVED", 45), (415, "push", "Worker đã push 1 commit", 30),
            (None, "incident", "Hết limit, tạm dừng giao việc 10 phút", 15)):
        store.log(pr, kind, text, now - timedelta(minutes=ago), f"https://github.com/acme/shop/pull/{pr}" if pr else "")
    (cfg.data_dir / "backlog.md").write_text(
        "# Backlog\n\n- [spec-gap] PR #413 · 2026-11-12 · Spec chưa nói ai được xóa đơn đã giao\n"
        "- [scope] PR #412 · 2026-11-12 · Tách luôn module báo cáo (ngoài phạm vi PR này)\n", encoding="utf-8")
    run_out = root / "run.json"
    run_out.write_text(json.dumps({"total_cost_usd": 0.37, "result": "ok"}))
    wts = {b: Worktree(b, str(root / "wt" / b.replace("/", "-"))) for b in DemoTransport.STATES}
    store.kv_set(f"run:{wts['fix/null-guard'].path}", json.dumps(
        {"batch": 3, "session": "c1d2", "started_at": (now - timedelta(minutes=40)).isoformat(), "out": str(run_out)}))
    store.kv_set("attempt:3", "2")
    store.kv_set(f"run:{wts['feat/orders-cancel'].path}", json.dumps(
        {"batch": 1, "session": "9f3c", "started_at": (now - timedelta(minutes=5)).isoformat()}))
    store.kv_set("slots_working", "1")
    store.close()
    return cd, (lambda c, s: DemoTransport()), (lambda c: wts), root
