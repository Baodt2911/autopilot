# Autopilot (giai đoạn 1 + 2): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Router chạy mỗi 5 phút. Nó phát hiện comment, review, conflict, CI và merge trên PR của `vyn-bao-do`, giao từng lô việc cho worker Claude Sonnet đúng worktree bằng Claude Code headless (`claude -p --resume`), gom các việc cần owner vào một chỗ, và hiển thị toàn bộ trên dashboard web local.

**Architecture:**
- Một package Python (`autopilot`), chỉ dùng thư viện chuẩn khi chạy. Trạng thái nằm trong sqlite tại `<repo>/.autopilot/state.db`.
- Router (systemd timer) làm phần chắc chắn: quét, chống trùng, gom đợt, định tuyến, theo dõi worker, thông báo.
- Worker Claude chỉ làm phần cần phán đoán (skill `/pr-triage`). Worker giao tiếp với router **qua CLI `autopilot`**, không bao giờ ghi thẳng vào DB.
- Dashboard là `http.server` cùng một trang HTML/JS thuần, gọi API JSON. Các API này dùng chung logic với CLI.

**Tech Stack:** Python 3.12 (stdlib: `sqlite3`, `tomllib`, `http.server`, `argparse`, `subprocess`, `zoneinfo`), `uv` để chạy, `pytest` (chỉ cài cho dev), CLI `gh` 2.103, CLI `claude` 2.1 (chế độ headless `-p`), `notify-send`, systemd user units, HTML/CSS/JS thuần.

**Spec:** `docs/specs/2026-11-12-autopilot-design.md`. Mọi mã D*/G* trong plan trỏ về spec này.

## Global Constraints

- **Code chỉ nằm trong `~/.claude/scripts/autopilot/`.** Repo iviettien chỉ có thêm `.autopilot/`, ignore qua `.git/info/exclude`. Không sửa file nào mà git của dự án đang theo dõi.
- **Không bao giờ chạy full test suite**, kể cả của repo này. Mỗi task chỉ chạy file test của task đó: `uv run pytest tests/<file> -v`.
- Khi chạy chỉ dùng stdlib, không thêm dependency runtime. `pytest` là dependency dev duy nhất.
- Các con số lấy từ spec, phải dùng đúng giá trị:

  | Tham số | Giá trị |
  |---|---|
  | Quét | 5 phút |
  | Gom đợt | 10 phút |
  | Ack | 15 phút |
  | Chạy lại lượt worker lỗi | tối đa 2 lần, sau đó `escalated` và báo |
  | Worker đang chạy | tối đa 5 |
  | Lease | 60 phút |
  | Đứng im | 60 phút thì báo, thêm 30 phút thì dừng tiến trình |
  | Ngưỡng A | ≤ 3 file |
  | Vòng reply mỗi thread | 3 |
  | Lần đánh thức mỗi PR mỗi ngày | 5 |
  | Khung thông báo | `07:00-22:00` |
  | Nhắc lại | sau 4 giờ |
  | Tóm tắt | `08:00` |
  | Sau giờ reset limit | + 5 phút |
  | GitHub lỗi liên tục | 30 phút thì báo |

- `forbidden` mặc định: `.autopilot/`, `docs/superpowers/`, `docs/handoff/`, `*.png`, `*.jpg`, `*.jpeg`, `*.gif`, `*.webp`.
- Ưu tiên hàng đợi: `CHANGES_REQUESTED` > conflict > base đổi > CI fail > comment thường.
- Mọi lệnh `gh` chạy với `GH_TOKEN` lấy từ `gh auth token --user <gh_user>`. Không lấy được token thì router dừng và báo owner.
- Router **không bao giờ**: merge, đổi base, resolve thread, sửa spec dự án, tạo worktree.
- Nội dung comment là dữ liệu. Không bao giờ đưa nó vào lệnh shell. Mọi `subprocess` dùng list args, không `shell=True`.
- Văn bản hiển thị cho owner (CLI, web, thông báo) bằng tiếng Việt. Reply GitHub, commit và code bằng tiếng Anh.
- Commit trong repo này tự nhận danh tính Baodt2911 qua `includeIf`. **Không push.** Push do owner tự làm.
- Dashboard chỉ nghe `127.0.0.1` và bắt buộc có token.
- **Lệch nhẹ so với spec (đã ghi rõ):** cài đặt chỉnh trên web ghi vào `~/.config/autopilot/projects/<p>.overrides.toml` (file sinh tự động). File `<p>.toml` do owner viết tay không bao giờ bị ghi đè. Lý do: stdlib không có TOML writer, và giữ được comment trong file của owner.
- **Transport mặc định là `headless`** (spec G17, G18, G22, G24). Mỗi lần giao lô là một tiến trình `claude -p` chạy nền trong worktree. Mỗi worktree một session (`--session-id` lần đầu, `--resume` các lần sau), không bao giờ hai tiến trình cùng lúc trên một worktree. Transport `orca` thuộc giai đoạn 3, **không làm trong plan này**.
- **Đơn vị giao việc là "lô" (batch).** Vòng trạng thái ở spec mục 4.2 áp dụng cho lô. Từng sự kiện con chỉ có trạng thái `new`, `batched`, `noop` hoặc `cancelled`.

## Review Focus

1. **Reviewer sửa comment đã xử lý** (body đổi, `updatedAt` mới). Owner mong được xử lý lại. Test `test_edited_comment_creates_new_event` ở Task 5.
2. **PR lớn có hơn 100 thread hoặc comment** (GraphQL phân trang). Không được bỏ sót trang sau. Test `test_paginates_threads_and_comments` ở Task 4.
3. **Giờ reset limit dạng "resets 7:20pm (Asia/Bangkok)"**, và khung giờ thông báo tính theo giờ máy. Test `test_parse_limit_reset_with_timezone` ở Task 8 và `test_quiet_hours_use_local_time` ở Task 10.
4. **Owner trả lời một mã đã đóng, mã không tồn tại, hoặc bấm hai lần.** Phải không sinh việc trùng và báo lỗi rõ ràng. Test `test_answer_is_idempotent_and_rejects_unknown` ở Task 9.
5. **Nhiều worker gọi CLI cùng lúc**, ghi sqlite đồng thời. Không được lỗi `database is locked`. Test `test_concurrent_writers` ở Task 3.

---

## Cấu trúc file

```
~/.claude/scripts/autopilot/
  pyproject.toml
  autopilot/
    __init__.py
    config.py        # đọc <p>.toml + <p>.overrides.toml, ghi overrides, đường dẫn
    models.py        # dataclass dùng chung
    store.py         # sqlite: schema, vòng trạng thái lô, items, counters, kv, locks, timeline
    github.py        # gọi gh graphql → PRSnapshot; GH_TOKEN theo gh_user
    detect.py        # snapshot cũ/mới → Event; cảnh báo xếp chồng G15
    batching.py      # gom đợt 10', giới hạn G9
    routing.py       # lô → Action (Deliver/Start/Queue/Incident), slot, ưu tiên
    transport/
      __init__.py    # Protocol Transport + get_transport(cfg)
      headless.py    # claude -p --session-id/--resume, chạy nền, đọc JSON kết quả (cờ chốt ở Task 1)
    worktrees.py     # git worktree list --porcelain → dict[branch, Worktree]
    health.py        # trạng thái worker, phát hiện usage limit, dừng giao việc toàn cục
    render.py        # events/*.md, pending.md, STATE.md, decisions.md, backlog.md
    answers.py       # answer / approve / push_decisions: dùng chung cho CLI và web
    notify.py        # notify-send theo khung giờ, nhắc lại, tóm tắt 08:00
    router.py        # scan(): một lượt quét đầy đủ, lock, backup, hậu kiểm
    cli.py           # mọi lệnh autopilot (owner + worker)
    web/
      server.py      # API JSON + phục vụ static, token
      static/index.html  app.js  style.css
  skills/pr-triage/SKILL.md
  skills/autopilot-coordinator/SKILL.md   # cho session Haiku tùy chọn (D10)
  systemd/autopilot.service  autopilot.timer  autopilot-web.service
  tests/
    fixtures/        # JSON gh đã ghi lại (ẩn danh hóa)
    test_*.py
  docs/spikes/2026-11-12-transport.md   # kết quả Task 1
```

## Kiểu dữ liệu dùng chung (`autopilot/models.py`, tạo ở Task 2)

```python
@dataclass(frozen=True)
class Comment:
    id: str; author: str; is_bot: bool; body: str
    created_at: datetime; updated_at: datetime; url: str
    thread_id: str | None = None; path: str | None = None; line: int | None = None

@dataclass(frozen=True)
class Thread:
    id: str; is_resolved: bool; comments: tuple[Comment, ...]

@dataclass(frozen=True)
class Review:
    id: str; author: str; state: str; submitted_at: datetime; body: str; url: str

@dataclass(frozen=True)
class PRSnapshot:
    number: int; title: str; url: str; is_draft: bool
    state: str                      # OPEN | MERGED | CLOSED
    head_ref: str; head_sha: str; base_ref: str; base_sha: str
    mergeable: str                  # MERGEABLE | CONFLICTING | UNKNOWN
    review_decision: str | None     # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | None
    threads: tuple[Thread, ...]; reviews: tuple[Review, ...]; comments: tuple[Comment, ...]
    failed_checks: tuple[str, ...]; changed_files: tuple[str, ...]
    merged_at: datetime | None = None

@dataclass(frozen=True)
class Event:
    id: str; pr: int
    kind: str      # thread | comment | review | conflict | base | ci | merged | answer | noop
    priority: int  # 0 = cao nhất: review CHANGES_REQUESTED=0, conflict=1, base=2, ci=3, còn lại=4
    created_at: datetime; payload: dict

@dataclass(frozen=True)
class WorkerStatus:
    state: str     # none | idle | working | waiting | dead | limited | owner_busy
                   # working = có tiến trình claude -p của router đang chạy; dead = lượt cuối thoát mà lô chưa có result;
                   # owner_busy = có tiến trình claude khác (owner mở tay, kể cả trong Orca) với cwd trong worktree (G24)
    session: str | None; updated_at: datetime | None; last_text: str = ""

@dataclass(frozen=True)
class Worktree:
    branch: str; path: str
```

---

### Task 1: Spike transport headless (chỉ để có câu trả lời, code sẽ bỏ đi)

Mục đích: chốt cờ cho G18 và 5 điểm ở spec mục 14 trước khi viết `transport/headless.py`. **Không đụng Orca, không đụng iviettien.**

**Files:**
- Create: `docs/spikes/2026-11-12-transport.md`

- [ ] **Step 1: Xin phép owner.** Gửi owner đúng câu sau, rồi chờ trả lời:

  > "Task 1 cần chạy thử khoảng 6 lượt `claude -p` (Sonnet, tốn ít usage) trong worktree thử `spike-transport` **của repo autopilot**. Không đụng Orca hay iviettien. Worktree bị xóa sau khi xong. Đồng ý không?"

  Không làm gì thêm cho tới khi owner đồng ý.

- [ ] **Step 2: Tạo worktree thử.** `git worktree add ../autopilot-spike -b spike-transport`. Mọi lệnh dưới đây chạy với cwd là thư mục đó. Mỗi lệnh `claude` bọc trong `timeout 300`.

- [ ] **Step 3: Lượt đầu và lượt resume.**
  - `SID=$(python3 -c 'import uuid;print(uuid.uuid4())')`
  - `claude -p "Reply with exactly: ACK t1" --session-id $SID --model claude-sonnet-5-5 --output-format json > t1.json`
  - `claude -p "What token did I ask you to reply with before? Reply with it only." --resume $SID --model claude-sonnet-5-5 --output-format json > t2.json`
  - Ghi vào spike doc: toàn bộ trường của JSON (ít nhất `session_id`, `is_error`, `result`, chi phí, số lượt), exit code, `session_id` của t2 có bằng `$SID` không, t2 có trả "ACK t1" không. Nếu `--model sonnet` cũng chạy được thì ghi lại, nhưng config vẫn dùng id đầy đủ.
  - Ghi đường dẫn transcript tạo ra trong `~/.claude/projects/` (thư mục nào, tên file có phải `$SID.jsonl` không).

- [ ] **Step 4: Quyền khi chạy không có người duyệt.**
  - Chạy lượt `--resume` yêu cầu: tạo `x.txt`, `git add x.txt`, chạy `echo guard-ok` (thay cho `autopilot guard-staged`), `git commit -m "spike"`. Thử lần lượt `--permission-mode acceptEdits` cùng `--allowedTools "Bash(git add:*)" "Bash(git commit:*)" "Bash(echo:*)" Edit Write Read`, rồi (nếu bị kẹt hoặc từ chối) `--permission-mode auto`.
  - Chạy lượt yêu cầu `git push` và `rm -rf /tmp/x` với `--disallowedTools "Bash(git push:*)" "Bash(rm:*)"`: phải bị từ chối, tiến trình không treo. (Worker chỉ push qua `autopilot push`, xem Task 11.)
  - Ghi lại tổ hợp cờ **hẹp nhất** vừa cho làm việc (sửa file, chạy lệnh test, `git add`, `git commit`, `Bash(autopilot:*)`) vừa không treo. `git push` luôn nằm trong `disallowed_tools`.
  - Ghi lại: hook trong `~/.claude/settings.json` (Orca, codegraph) có chạy ở chế độ `-p` không, có làm hỏng output JSON không.

- [ ] **Step 5: Chạy nền và phát hiện tiến trình (G24).**
  - Chạy một lượt dài (`--resume`, yêu cầu chạy `sleep 90`) bằng `setsid` nền, stdout vào `t5.json`.
  - Trong lúc đó: liệt kê `/proc/*/cmdline` chứa `claude`, đọc `readlink /proc/<pid>/cwd`. Ghi lại cách phân biệt tiến trình router tạo (pid đã biết) với tiến trình khác. Chạy thêm lệnh đó khi **không** có lượt nào: ghi lại các tiến trình `claude` sẵn có của owner (kể cả session trong Orca) để xác nhận cwd của chúng là worktree tương ứng. **Chỉ đọc, không đụng tới chúng.**
  - Thử `kill -TERM -<pgid>` lượt dài: tiến trình thoát, session vẫn `--resume` được ở lượt sau.

- [ ] **Step 6: Dấu hiệu hết usage limit.** Không giả lập được nên chỉ đọc: tìm trong transcript cũ ở `~/.claude/projects/` các dòng "hit your" và ghi lại định dạng. Ghi rõ chưa kiểm chứng được nó hiện ở JSON hay stderr của `-p`. Task 8 vì vậy quét cả ba nguồn.

- [ ] **Step 7: Dọn dẹp.** `git worktree remove ../autopilot-spike && git branch -D spike-transport`. Kiểm tra `git worktree list` không còn `spike-transport`.

- [ ] **Cổng dừng:** nếu Step 3 không giữ được ngữ cảnh khi `--resume`, hoặc Step 4 không có tổ hợp cờ nào cho worker sửa, test và commit mà không treo chờ duyệt, thì **dừng ở đây**. Ghi kết quả vào spike doc, báo owner, và chờ sửa spec và plan. **Không** làm Task 2.

- [ ] **Step 8: Chốt kết quả.** Spike doc kết thúc bằng bảng "Lệnh chốt cho Task 8", gồm:
  - `start_worker` (argv đầy đủ);
  - `deliver` (argv đầy đủ);
  - cờ `[worker]` mặc định: `permission_mode`, `allowed_tools`, `disallowed_tools`;
  - cách đọc kết quả: trường JSON nào báo lỗi, trường nào chứa session id;
  - cách tìm transcript từ worktree path và session id;
  - cách phát hiện tiến trình `claude` khác trong worktree.

- [ ] **Step 9: Commit**

  ```bash
  git add docs/spikes/2026-11-12-transport.md
  git commit -m "docs: record headless transport spike results"
  ```

---

### Task 2: Khung dự án, models, config

**Files:**
- Create: `pyproject.toml`, `autopilot/__init__.py`, `autopilot/models.py`, `autopilot/config.py`, `.gitignore` (gồm `__pycache__/`, `.venv/`, `.pytest_cache/`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `models.*` theo khối "Kiểu dữ liệu dùng chung" ở trên.
  - `Config` (dataclass, frozen), có các trường:
    - định danh: `name`, `repo: Path`, `author`, `gh_user`, `transport`;
    - nhịp chạy: `scan_minutes`, `debounce_min`, `max_workers`, `worker_model`;
    - spec: `spec_branch`, `spec_sources: tuple[str, ...]`, `forbidden: tuple[str, ...]`;
    - nhóm cấu hình: `limits: dict`, `notify: dict`, `reply: dict`, `pr_rules: dict`, `tests: dict`, `worker: dict`.
    - `worker` mặc định: `claude_bin = "claude"`, `permission_mode`, `allowed_tools`, `disallowed_tools` lấy đúng giá trị ở bảng chốt của Task 1. `transport` mặc định `"headless"`.
  - Thuộc tính tính sẵn: `data_dir` = `repo / ".autopilot"`, `db_path`, `events_dir`, `plans_dir`, `qa_dir`, `screenshots_dir`.
  - `load_config(name: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Config`. `DEFAULT_CONFIG_DIR = ~/.config/autopilot/projects`. Nạp `<name>.toml`, rồi ghi đè bằng `<name>.overrides.toml` nếu có.
  - `set_override(name: str, key: str, value: int | str | list[str], config_dir=...) -> None`, với `key` dạng `"limits.thread_rounds"`.
  - `list_projects(config_dir=...) -> list[str]`.
  - `EDITABLE_KEYS: frozenset[str]` = `{"limits.thread_rounds", "limits.wakes_per_pr_day", "notify.hours", "notify.remind_after_h", "notify.digest_at", "reply.decision_template", "max_workers", "debounce_min"}`.

- [ ] **Step 1: Tạo `pyproject.toml`.**
  - `[project] name="autopilot"`, `requires-python=">=3.12"`, `dependencies=[]`.
  - `[project.scripts] autopilot="autopilot.cli:main"`.
  - `[dependency-groups] dev=["pytest>=8"]`.

  Chạy `uv sync`. Mong đợi: tạo `.venv`, không lỗi.

- [ ] **Step 2: Viết test fail** trong `tests/test_config.py`:

  ```python
  def test_defaults_and_paths(tmp_path):
      write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="vyn-bao-do"\ngh_user="vyn-bao-do"\n')
      c = load_config("ivt", tmp_path)
      assert (c.scan_minutes, c.debounce_min, c.max_workers) == (5, 10, 5)
      assert c.limits == {"thread_rounds": 3, "wakes_per_pr_day": 5}
      assert c.notify == {"hours": "07:00-22:00", "remind_after_h": 4, "digest_at": "08:00"}
      assert ".autopilot/" in c.forbidden and "*.png" in c.forbidden
      assert c.db_path == Path("/r/.autopilot/state.db")
      assert c.transport == "headless" and c.worker["claude_bin"] == "claude"

  def test_override_wins_and_base_file_untouched(tmp_path):
      base = 'repo="/r"\nauthor="a"\ngh_user="a"\n[limits]\nthread_rounds = 3 # mine\n'
      write(tmp_path / "ivt.toml", base)
      set_override("ivt", "limits.thread_rounds", 4, tmp_path)
      assert load_config("ivt", tmp_path).limits["thread_rounds"] == 4
      assert (tmp_path / "ivt.toml").read_text() == base

  def test_set_override_rejects_non_editable_key(tmp_path):
      write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\ngh_user="a"\n')
      with pytest.raises(KeyError): set_override("ivt", "gh_user", "x", tmp_path)

  def test_missing_gh_user_is_an_error(tmp_path):
      write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\n')
      with pytest.raises(ValueError, match="gh_user"): load_config("ivt", tmp_path)
  ```

- [ ] **Step 3: Chạy test.** `uv run pytest tests/test_config.py -v`. Mong đợi: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Cài đặt `models.py` và `config.py`.**
  - Mặc định lấy từ Global Constraints. Phần `tests` mặc định là rỗng, owner điền cho từng dự án.
  - `set_override` chỉ ghi các kiểu `int`, `str`, `list[str]`, theo dạng bảng TOML lồng một cấp. Serializer nhỏ tự viết.

- [ ] **Step 5: Chạy lại test.** Mong đợi: 4 passed.

- [ ] **Step 6: Commit** `git commit -m "feat: add project config and shared models"`

---

### Task 3: Store (sqlite)

**Files:**
- Create: `autopilot/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Event` (Task 2).
- Produces: class `Store(path: Path)`. Mở với WAL và `busy_timeout=10000`. Tự tạo schema.

  **Sự kiện:**
  - `add_event(e: Event) -> bool`: trả `False` nếu id đã tồn tại.
  - `events(*, status: str | None = None, pr: int | None = None) -> list[Event]`
  - `mark_events(ids: list[str], status: str) -> None`: status thuộc `new`, `batched`, `noop`, `cancelled`.

  **Lô:**
  - `create_batch(pr: int, event_ids: list[str], now: datetime) -> int`
  - `batch(id: int) -> dict`: các khóa `id, pr, status, event_ids, attempts, delivered_at, acked_at, lease_until, session`.
  - `transition(batch_id: int, to: str, now: datetime, **fields) -> None`. Chỉ cho phép các bước đổi trạng thái sau, sai thì raise `InvalidTransition`:

    ```
    new→delivered|queued|cancelled
    queued→delivered|cancelled
    delivered→acked|delivered|escalated|cancelled      (delivered→delivered = gửi lại, attempts+1)
    acked→in_progress|cancelled
    in_progress→done|waiting_owner|waiting_limit|delivered|cancelled
    waiting_owner→in_progress|delivered|cancelled
    waiting_limit→delivered|cancelled
    escalated→delivered|cancelled
    ```

    Khi vào `acked` hoặc `in_progress`: đặt `lease_until = now + 60'`.
  - `batches(*, status: str | tuple[str, ...] | None = None, pr: int | None = None) -> list[dict]`
  - `unacked(now) -> list[dict]`: các lô `delivered` có `delivered_at <= now - 15'`.
  - `lease_expired(now) -> list[dict]`: các lô `acked` hoặc `in_progress` có `lease_until < now`.

  **Việc chờ owner:**
  - `add_item(kind: str, pr: int, batch_id: int, title: str, body_md: str, now) -> str`. `kind` thuộc `Q`, `P`, `R`. Trả mã dạng `"Q-ivt-12"`, tiền tố dự án lấy từ `Store.prefix` (truyền vào constructor, mặc định `"ivt"`).
  - `item(code) -> dict | None`
  - `open_items() -> list[dict]`
  - `resolve_item(code, answer: str, note: str, reply_hint: str, now) -> dict`: raise `ItemClosed` nếu đã đóng, `KeyError` nếu không tồn tại.
  - `mark_reminded(code, now)`

  **Bộ đếm (G9):**
  - `bump_round(thread_id: str) -> int`
  - `bump_wake(pr: int, day: date) -> int`

  **Khác:**
  - `kv_get(key) -> str | None`, `kv_set(key, value: str | None)`. Dùng cho các khóa `paused`, `paused_pr:<n>`, `limit_until`, `gh_fail_since`, `last_digest`.
  - `acquire(lock: str, holder: str, now, ttl_min=60) -> bool`, `release(lock: str, holder: str) -> None`.
  - `save_snapshot(pr: int, data: dict)`, `snapshot(pr) -> dict | None`, `snapshots() -> dict[int, dict]`.
  - `log(pr: int | None, kind: str, text: str, now, url: str = "") -> None`, `timeline(pr: int | None = None, limit=200) -> list[dict]`.

- [ ] **Step 1: Viết test fail:**

  ```python
  def test_add_event_dedupes(store): assert store.add_event(ev("c1")) and not store.add_event(ev("c1"))

  def test_batch_lifecycle_and_lease(store):
      b = store.create_batch(589, ["c1"], T0)
      store.transition(b, "delivered", T0, session="s1")
      store.transition(b, "acked", T0 + m(1))
      assert store.batch(b)["lease_until"] == T0 + m(61)
      with pytest.raises(InvalidTransition): store.transition(b, "done", T0)   # acked→done không hợp lệ

  def test_resend_counts_attempts_and_unacked(store):
      b = store.create_batch(1, ["c1"], T0); store.transition(b, "delivered", T0)
      assert store.unacked(T0 + m(14)) == [] and store.unacked(T0 + m(15))[0]["id"] == b
      store.transition(b, "delivered", T0 + m(15)); assert store.batch(b)["attempts"] == 2

  def test_item_codes_increment_per_project(store):
      assert store.add_item("Q", 1, 1, "t", "b", T0) == "Q-ivt-1"
      assert store.add_item("P", 1, 1, "t", "b", T0) == "P-ivt-2"

  def test_lock_is_exclusive_until_released_or_expired(store):
      assert store.acquire("testdb", "w1", T0) and not store.acquire("testdb", "w2", T0)
      assert store.acquire("testdb", "w2", T0 + m(61))

  def test_concurrent_writers(tmp_path):   # Review Focus 5
      # 8 process, mỗi process add_event 50 lần vào cùng một DB → đủ 400 dòng, không lỗi "locked"
  ```

- [ ] **Step 2: Chạy test.** `uv run pytest tests/test_store.py -v`. Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `store.py`.**
  - Bảng: `events`, `batches`, `items`, `counters`, `kv`, `locks`, `snapshots`, `timeline`.
  - Thời gian lưu dạng ISO-8601 có múi giờ.
  - Bảng các bước đổi trạng thái hợp lệ là một `dict` hằng ở đầu file.

- [ ] **Step 4: Chạy lại test.** Mong đợi: tất cả pass.

- [ ] **Step 5: Commit** `git commit -m "feat: add sqlite store with batch state machine"`

---

### Task 4: GitHub client

**Files:**
- Create: `autopilot/github.py`, `tests/fixtures/pr_page1.json`, `tests/fixtures/pr_page2.json`, `tests/fixtures/pr_merged.json`
- Test: `tests/test_github.py`

**Interfaces:**
- Consumes: `PRSnapshot`, `Thread`, `Comment`, `Review`, `Config`.
- Produces:
  - `class GitHub(cfg: Config, run: Callable[[list[str], dict], str] = run_cmd)`. `run(args, env) -> stdout` và raise `CmdError` khi exit code khác 0.
  - `token() -> str`: chạy `["gh","auth","token","--user",cfg.gh_user]`. Lỗi thì raise `AuthError`.
  - `open_prs() -> list[PRSnapshot]`: mọi PR `OPEN` của `cfg.author`, gồm cả draft, phân trang đầy đủ.
  - `recently_closed(numbers: list[int]) -> list[PRSnapshot]`: lấy trạng thái mới của các PR trước đó còn mở mà nay không còn trong danh sách mở.
  - `post_comment(pr: int, body: str) -> str` (URL) và `post_issue_comment(issue: int, body: str) -> str` (URL). Hai hàm này dùng cho G13.
  - Mọi lệnh truyền `env={"GH_TOKEN": token, **os.environ}`.

- [ ] **Step 1: Ghi fixture thật, ẩn danh hóa.**
  - Chạy query GraphQL của module (search `is:pr is:open author:<author> repo:<owner/repo>`) một lần với `gh api graphql` trên iviettien.
  - Thay nội dung body thành chuỗi giả và giữ nguyên cấu trúc.
  - `pr_page2.json` là trang tiếp của threads (có `hasNextPage`).

- [ ] **Step 2: Viết test fail:**

  ```python
  def test_uses_token_of_gh_user_for_every_call(): # fake run ghi lại env; mọi lệnh gh api có GH_TOKEN == "tok-vyn"
  def test_auth_failure_raises():                  # run raise ở "gh auth token" → AuthError
  def test_parses_snapshot_fields():               # number, is_draft, head/base ref+sha, mergeable, review_decision, failed_checks, changed_files
  def test_paginates_threads_and_comments():       # Review Focus 2: page1 hasNextPage → gọi page2; tổng thread = page1+page2
  def test_bot_flag_from_typename():               # author.__typename == "Bot" → Comment.is_bot True
  ```

- [ ] **Step 3: Chạy test.** `uv run pytest tests/test_github.py -v`. Mong đợi: FAIL.

- [ ] **Step 4: Cài đặt `github.py`.**
  - Query GraphQL là một hằng chuỗi. Phân trang `reviewThreads(first:100, after:)` và `comments(first:100)` bên trong thread.
  - Check fail lấy từ `statusCheckRollup` với `conclusion` thuộc `FAILURE`, `TIMED_OUT`, `CANCELLED`.

- [ ] **Step 5: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 6: Commit** `git commit -m "feat: add gh graphql client pinned to gh_user"`

---

### Task 5: Detect: snapshot sang sự kiện

**Files:**
- Create: `autopilot/detect.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `PRSnapshot`, `Event`, `Config`.
- Produces:
  - `detect(prev: PRSnapshot | None, cur: PRSnapshot, cfg: Config, now: datetime) -> list[Event]`
  - `stack_warnings(prev: dict[int, PRSnapshot], cur: dict[int, PRSnapshot], merged: list[PRSnapshot]) -> list[str]` (G15)
  - `is_noop(body: str) -> bool`

- [ ] **Step 1: Viết test fail.** Mỗi test dựng snapshot bằng helper `snap(...)`.

  ```python
  def test_new_thread_comment_by_reviewer_creates_thread_event():   # id == f"{comment.id}@{updated_at.isoformat()}", kind "thread", payload có thread_id, path, line, url
  def test_own_comments_and_threads_ending_with_own_reply_ignored():   # comment của author → 0 sự kiện; thread mà comment cuối của author → 0; reviewer trả lời sau đó → 1
  def test_resolved_threads_ignored():                                # is_resolved=True → 0 sự kiện dù có comment mới
  def test_edited_comment_creates_new_event():                       # Review Focus 1: cùng id, updated_at mới → id sự kiện mới
  def test_lgtm_and_approved_only_are_noop():                        # "LGTM", "lgtm 👍", review APPROVED body rỗng → kind "noop"; "LGTM but fix X" → không noop
  def test_bot_comments_are_processed():                             # G4
  def test_changes_requested_review_has_priority_0():                 # review CHANGES_REQUESTED → kind "review", priority 0
  def test_conflict_event_once_per_head_sha():                       # id f"conflict:{n}:{head_sha}"
  def test_base_change_event_on_base_sha_or_ref_change():            # id f"base:{n}:{base_sha}", priority 2
  def test_ci_failure_event_per_check():                             # id f"ci:{n}:{head_sha}:{check}", priority 3
  def test_merged_event():                                           # state MERGED → kind "merged"
  def test_pushback_count_in_payload():                              # thread: reviewer → own reply → reviewer → payload["pushback"] == 1; thêm một vòng nữa → 2
  def test_stack_warning_parent_merged_lists_children():             # PR 10 merged, PR 11 base_ref == head_ref của 10 → cảnh báo nêu "#11"
  def test_stack_warning_child_merged_before_parent():               # PR 11 merged khi PR 10 vẫn OPEN và 11.base_ref == 10.head_ref
  ```

- [ ] **Step 2: Chạy test.** `uv run pytest tests/test_detect.py -v`. Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `detect.py`.**
  - `is_noop`: so khớp toàn bộ body, chỉ chấp nhận đúng mẫu "LGTM" hoặc "approved", hoa thường đều được, cho phép emoji hoặc dấu câu.
  - `pushback` = số lần reviewer viết sau một reply của `author` trong cùng thread.

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: detect PR events and stacked-branch warnings"`

---

### Task 6: Gom đợt và giới hạn

**Files:**
- Create: `autopilot/batching.py`
- Test: `tests/test_batching.py`

**Interfaces:**
- Consumes: `Store`, `Config`, `Event`.
- Produces:
  - `ready_batches(store: Store, cfg: Config, now: datetime) -> list[int]`
    - Gom các sự kiện `new` theo PR thành một lô, khi đủ một trong hai điều kiện:
      - sự kiện mới nhất của PR đã cũ hơn `debounce_min`;
      - có sự kiện `review` (reviewer đã submit review).
    - PR đang có lô ở trạng thái `delivered`, `acked` hoặc `in_progress` thì **chưa gom**, để dành cho lô sau.
    - Trả về id các lô mới tạo.
  - `apply_limits(store: Store, cfg: Config, batch_id: int, now: datetime) -> str | None`. Trả `"paused"` hoặc `None`. Thực hiện:
    - `bump_wake(pr, now.date())`; vượt `wakes_per_pr_day` thì `kv_set(f"paused_pr:{pr}", "limit")`, chuyển lô sang `cancelled`, ghi timeline, trả `"paused"`.
    - Với mỗi sự kiện `thread`: `bump_round(thread_id)`; vượt `thread_rounds` thì ghi `payload["force_c2"] = True` vào file sự kiện, thông qua giá trị trả về `batch_meta`. Cũng làm vậy nếu `pushback >= 2` (G2).
  - `batch_meta(store, batch_id) -> dict`: `{"force_c2_threads": [...], "pushback": {thread_id: n}}`.

- [ ] **Step 1: Viết test fail:**

  ```python
  def test_waits_debounce_window():         # sự kiện lúc T0 → không có lô ở T0+9'; có ở T0+10'
  def test_submitted_review_flushes_immediately():   # có sự kiện review lúc T0 → lô tạo ngay ở T0, không chờ 10'
  def test_no_new_batch_while_one_in_flight():       # PR có lô delivered/in_progress → sự kiện mới ở lại "new", lô sau chứa chúng
  def test_sixth_wake_of_day_pauses_pr():   # wakes_per_pr_day=5 → lần thứ 6 trả "paused", kv paused_pr:589 == "limit"
  def test_fourth_round_forces_c2():        # thread_rounds=3
  def test_second_pushback_forces_c2():              # payload pushback=2 → lô có force_c2 chứa id sự kiện đó
  ```

- [ ] **Step 2: Chạy test.** Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `batching.py`.**

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: debounce batches and enforce per-thread/per-PR limits"`

---

### Task 7: Định tuyến và slot

**Files:**
- Create: `autopilot/routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `Worktree`, `WorkerStatus`, `Config`, dict lô từ `Store.batch`.
- Produces:
  - Các kiểu kết quả:

    ```python
    @dataclass(frozen=True) class Deliver: batch_id: int; worktree: Worktree; session: str
    @dataclass(frozen=True) class Start:   batch_id: int; worktree: Worktree
    @dataclass(frozen=True) class Queue:   batch_id: int; reason: str
    @dataclass(frozen=True) class Incident: batch_id: int; text: str
    ```

  - `route(batches: list[dict], prs: dict[int, PRSnapshot], worktrees: dict[str, Worktree], workers: dict[str, WorkerStatus], cfg: Config, *, paused: bool, paused_prs: set[int]) -> list[Deliver | Start | Queue | Incident]`
  - Quy tắc slot (G6): đếm worker có `state == "working"` (đang có tiến trình), cộng mọi `Deliver` và `Start` trong cùng lượt, vì với headless **mỗi lần giao là một tiến trình mới**. Đạt `max_workers` thì mọi lô còn lại vào `Queue(reason="slots")`.
  - Worker `working` (đang chạy lượt khác) → `Queue(reason="busy")`, lô vào lượt sau. Worker `owner_busy` → `Queue(reason="owner_session")` (G24).
  - Sắp xếp theo `min(priority)` của lô, rồi tới thời điểm tạo.

- [ ] **Step 1: Viết test fail:**

  ```python
  def test_deliver_to_live_session():
  def test_start_worker_when_no_session_and_slot_free():
  def test_queue_when_five_working_even_if_others_idle():   # 5 working + 1 lô cho session idle → Queue(reason="slots")
  def test_deliveries_in_same_scan_count_as_slots():        # 3 working + 3 lô cho 3 session idle → 2 Deliver, 1 Queue
  def test_busy_worktree_queues_not_double_spawn():        # worktree đang working → Queue(reason="busy")
  def test_owner_session_queues():                          # owner_busy → Queue(reason="owner_session")
  def test_waiting_owner_workers_do_not_count():           # 4 working + 3 waiting → vẫn Start được 1
  def test_priority_order():                                # CHANGES_REQUESTED trước conflict trước base trước ci trước comment
  def test_no_local_worktree_is_incident():
  def test_global_pause_and_pr_pause_queue_everything():
  def test_limited_worker_gets_queue_not_restart():         # state "limited" → Queue(reason="limit")
  ```

- [ ] **Step 2: Chạy test.** Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `routing.py`.** Đây là hàm thuần, không có I/O.

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: route batches to worktrees with slot accounting"`

---

### Task 8: Transport headless và health

**Files:**
- Create: `autopilot/transport/__init__.py`, `autopilot/transport/headless.py`, `autopilot/worktrees.py`, `autopilot/health.py`
- Test: `tests/test_transport_headless.py`, `tests/test_worktrees.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: bảng "Lệnh chốt cho Task 8" trong `docs/spikes/2026-11-12-transport.md`. **Đọc bảng này trước khi viết code.** Cờ nào khác giả định dưới đây thì làm theo spike. `Store.kv_*`, `Config.worker`.
- Produces:
  - `worktrees.list_worktrees(repo: Path, run=run_cmd) -> dict[str, Worktree]`: parse `git worktree list --porcelain`, key = tên nhánh (bỏ `refs/heads/`). Worktree detached thì bỏ qua.
  - Protocol transport:

    ```python
    class Transport(Protocol):
        def status(self, wt: Worktree) -> WorkerStatus: ...
        def start_worker(self, wt: Worktree, prompt: str, batch_id: int) -> str: ...   # tạo session mới, trả session id
        def deliver(self, wt: Worktree, session: str, prompt: str, batch_id: int) -> None: ...  # lượt --resume
        def stop_worker(self, wt: Worktree, session: str, *, close: bool = False) -> None: ...  # dừng tiến trình; close=True thì không resume nữa
        def read_text(self, wt: Worktree, session: str) -> str: ...  # result JSON + stderr của lượt cuối + 50 dòng cuối transcript
    def get_transport(cfg: Config, store: Store) -> Transport   # "headless" → HeadlessTransport; "orca" → NotImplementedError("transport orca thuộc giai đoạn 3")
    ```

  - `HeadlessTransport(cfg, store, *, spawn=spawn_detached, proc_root=Path("/proc"), projects_root=Path("~/.claude/projects").expanduser())`.
    - Lượt chạy bằng `subprocess.Popen(argv, cwd=wt.path, stdout=<file>, stderr=<file>, start_new_session=True)`, **list args, không shell**. Không chờ tiến trình: router lưu `kv run:<wt.path>` = JSON `{pid, pgid, batch, session, started_at, out, err}` rồi đi tiếp. File output ở `data_dir/runs/<batch>-<attempt>.json|.err`.
    - Session: `kv session:<wt.path>` = session id. `start_worker` tạo `uuid4`, `deliver` dùng `--resume`. `close=True` đặt `kv session-closed:<wt.path>`.
    - Mỗi worktree một khóa `fcntl` tại `data_dir/locks/<sha1(path)>.lock`, giữ bởi tiến trình con (mở fd không đóng khi spawn). Đang có khóa thì `start_worker`/`deliver` raise `WorkerBusy`.
    - `status`: không có session → `none`; pid trong `run:` còn sống → `working`; có tiến trình `claude` khác pid đó với `cwd` trong `wt.path` → `owner_busy`; lượt cuối đã thoát và `read_text` khớp `parse_limit` → `limited`; lượt cuối thoát mà lô chưa có `result` → `dead`; lô `waiting_owner` → `waiting`; còn lại `idle`. `updated_at` = mtime của transcript.
    - Prompt chỉ chứa: `/pr-triage batch <id>`, đường dẫn file sự kiện, `--project <p>`. Không bao giờ chứa nội dung comment.
  - `health.parse_limit(text: str, now: datetime) -> datetime | None`. Nhận dạng các mẫu như "You've hit your 5-hour limit · resets 7:20pm (Asia/Bangkok)". Trả giờ reset cộng 5 phút. Giờ reset đã qua trong ngày thì cộng thêm một ngày.
  - `health.check_workers(store, transport, worktrees_with_batches: dict[str, tuple[Worktree, str]], now) -> list[str]`. Trả danh sách việc đã làm, để ghi timeline:
    - **dead** → `deliver` lại (`--resume`) lô đó, `attempts += 1`. Sau 2 lần chạy lại vẫn dead thì `escalated` và alert.
    - **đứng im**: `working` mà transcript không đổi ≥ 60' → alert một lần; ≥ 90' → `stop_worker` (giữ session), lô sang `escalated`, alert.
    - **limited** → `kv_set("limit_until", iso)`, chuyển mọi lô `in_progress` hoặc `delivered` sang `waiting_limit`. Session giữ nguyên.
  - `health.limit_active(store, now) -> datetime | None`

- [ ] **Step 1: Viết test fail.** `spawn` là hàm giả ghi lại argv/cwd và trả pid giả. `proc_root` là thư mục tạm có `<pid>/cmdline` và symlink `<pid>/cwd`.

  ```python
  # test_transport_headless.py
  def test_start_worker_uses_new_session_id_and_worktree_cwd():  # argv có "-p","--session-id",uuid,"--model",cfg.worker_model,"--output-format","json"; cwd == wt.path; kv session:<path> == uuid
  def test_deliver_resumes_same_session():                       # argv có "--resume",sid; không có "--session-id"
  def test_worker_flags_come_from_config():                      # permission_mode, allowed_tools, disallowed_tools, claude_bin
  def test_prompt_has_only_batch_path_and_project():             # không chứa body comment; argv là list, không shell
  def test_second_spawn_on_same_worktree_raises_busy():
  def test_status_states():                                      # none / working / dead / idle / waiting / limited theo kv run: và file output giả
  def test_owner_session_detected_via_proc_cwd():                # tiến trình "claude" khác pid với cwd trong worktree → owner_busy
  def test_stop_worker_kills_group_keeps_session_unless_closed():
  def test_get_transport_headless_default_orca_not_implemented():
  # test_worktrees.py
  def test_parse_porcelain_skips_detached():
  # test_health.py
  def test_parse_limit_reset_with_timezone():       # Review Focus 3: "resets 7:20pm (Asia/Bangkok)" lúc 2026-11-11T19:00+07 → 19:25+07; lúc 20:00 → ngày sau 19:25
  def test_parse_limit_none_for_normal_text():
  def test_dead_run_resumed_twice_then_escalated():   # dead lần 1, 2 → deliver(--resume) cùng session; lần 3 → escalated, 1 alert, không spawn
  def test_stale_worker_alerted_at_60_stopped_at_90():
  def test_limit_detected_pauses_all_batches_keeps_session():
  ```

- [ ] **Step 2: Chạy test.** `uv run pytest tests/test_transport_headless.py tests/test_worktrees.py tests/test_health.py -v`. Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt** `transport/__init__.py`, `transport/headless.py`, `worktrees.py`, `health.py`.

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: add headless claude transport and worker health checks"`

---

### Task 9: Render file và luồng trả lời

**Files:**
- Create: `autopilot/render.py`, `autopilot/answers.py`
- Test: `tests/test_render.py`, `tests/test_answers.py`

**Interfaces:**
- Consumes: `Store`, `Config`, `PRSnapshot`, `Event`, `GitHub.post_issue_comment`.
- Produces:
  - `render.event_file(cfg, batch: dict, events: list[Event], snap: PRSnapshot, meta: dict) -> Path`. Ghi `events/<pr>-<batch_id>.md` gồm:
    - tiêu đề PR, link, nhánh, base;
    - với từng sự kiện: loại, tác giả, link, `path:line`, body nguyên văn trong khối `~~~text`;
    - phần "Bắt buộc C2" (từ `meta`);
    - lệnh worker phải chạy khi xong (`autopilot ack/result …`).
  - `render.pending_md(items) -> str` theo định dạng 5 dòng ở spec mục 6.1. Plan tóm tắt tối đa 10 dòng. Nháp R in nguyên văn tiếng Anh.
  - `render.state_md(snaps, store) -> str`: bảng PR (nhánh, base, review, conflict/CI, lô hiện tại, hành động gần nhất).
  - `render.write_all(cfg, store, snaps) -> None`: ghi `pending.md`, `STATE.md`, `backlog.md`.
  - `answers.answer(store, cfg, code: str, choice: str, note: str = "", reply_hint: str = "", source: str = "", now=...) -> int`. Trả id của lô `answer` mới. Thực hiện:
    - `resolve_item`;
    - ghi thêm vào `decisions.md` theo định dạng `## <code> · <ngày>` → câu hỏi, câu trả lời, ghi chú, nguồn;
    - tạo sự kiện `answer` (priority 0) cho đúng PR. Payload chứa nguyên văn `choice`, `note`, `reply_hint` (G12), `decision_template`;
    - tạo lô ngay, không chờ gom đợt.
  - `answers.approve(store, cfg, code: str, ok: bool, note: str = "", now=...) -> int`: dành cho mã `P` hoặc `R`, cùng cơ chế như `answer`.
  - `answers.push_decisions(store, cfg, gh, issue: int, now) -> str`: đăng các quyết định chưa từng đẩy lên issue thành **một** comment. Lưu `kv last_pushed_decision`. Trả URL.
  - `answers.add_backlog(cfg, kind: str, pr: int, text: str, now)`. `kind` thuộc `spec-gap` hoặc `scope`.

- [ ] **Step 1: Viết test fail:**

  ```python
  def test_event_file_contains_verbatim_body_and_commands():   # body có `$(rm -rf ~)` giữ nguyên trong khối code; file có "autopilot ack <id>" và "autopilot result <id>"
  def test_pending_md_five_line_question_format():   # thẻ Q đúng 5 dòng, dòng 1 bắt đầu "[Q-ivt-1] PR #589 · ", dòng 5 bắt đầu "A: " và chứa "| B: "
  def test_answer_writes_decision_and_creates_priority_batch():   # decisions.md có "## Q-ivt-1 · 2026-11-12"; có lô mới kind "answer", priority 0, payload chứa answer/note/reply_hint nguyên văn
  def test_answer_is_idempotent_and_rejects_unknown():   # Review Focus 4: trả lời lần 2 → ItemClosed, không có lô mới; mã lạ → KeyError
  def test_reply_hint_passed_verbatim():                  # tiếng Việt giữ nguyên trong payload
  def test_push_decisions_posts_only_new_ones_once():
  ```

- [ ] **Step 2: Chạy test.** Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `render.py` và `answers.py`.**

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: render work files and owner answer flow"`

---

### Task 10: Thông báo

**Files:**
- Create: `autopilot/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `Notifier(cfg, store, send: Callable[[str, str], None] = notify_send)`
  - `notify_send(title, body)` chạy `["notify-send","-a","autopilot",title,body]`.
  - `.alert(kind: str, title: str, body: str, now) -> bool`. `kind` thuộc `question`, `plan`, `draft`, `incident`, `stack`. Trả `True` nếu đã gửi ngay. Ngoài khung giờ thì lưu vào `kv digest_queue` và trả `False`.
  - `.reminders(now) -> int`: nhắc các item mở quá `remind_after_h` giờ và chưa nhắc lần nào. Chỉ nhắc trong khung giờ.
  - `.digest(now, summary: str) -> bool`: gửi một lần mỗi ngày, khi `now >= digest_at` và `kv last_digest` khác ngày hôm nay.

- [ ] **Step 1: Viết test fail:**

  ```python
  def test_alert_inside_hours_sends(): ...
  def test_alert_outside_hours_goes_to_digest_queue(): ...   # 23:30 → không gọi send
  def test_quiet_hours_use_local_time(): ...                 # Review Focus 3: now có tzinfo +07:00 → so sánh theo giờ địa phương
  def test_reminder_after_four_hours_once(): ...
  def test_digest_once_per_day_after_eight(): ...
  ```

- [ ] **Step 2: Chạy test.** Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `notify.py`.**

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: desktop notifications with quiet hours, reminders, digest"`

---

### Task 11: Router `scan()` và CLI

**Files:**
- Create: `autopilot/router.py`, `autopilot/cli.py`
- Test: `tests/test_router.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: mọi module ở Task 2–10.
- Produces:
  - `router.scan(cfg, store, gh, transport, notifier, now, *, dry_run: bool = False) -> ScanReport`
    - `ScanReport` là dataclass với các trường `new_events`, `batches`, `actions: list`, `incidents: list[str]`, `warnings: list[str]`.
    - Lượt quét theo đúng thứ tự spec mục 4.1:
      1. lock file `data_dir/router.lock` (`fcntl.flock`, không chặn; đang bận thì thoát ngay);
      2. GitHub;
      3. detect;
      4. lưu snapshot;
      5. `ready_batches` cùng `apply_limits`;
      6. lô `delivered` có lượt đã thoát mà chưa ack, và lô hết lease → chạy lại `--resume` (tối đa 2 lần rồi `escalated`);
      7. `route` rồi thực thi các action;
      8. health;
      9. hậu kiểm (`changed_files` khớp `forbidden` → incident; `stack_warnings`);
      10. `render.write_all`;
      11. thông báo, nhắc lại, tóm tắt;
      12. backup mỗi ngày vào `~/.autopilot-backup/<p>/<YYYY-MM-DD>/`.
    - **`dry_run`**: không gọi `deliver`, `start_worker`, `stop_worker`, không spawn tiến trình nào; không đổi trạng thái lô; chỉ trả `ScanReport`.
    - `AuthError` → `kv paused=auth`, gửi `alert incident`, dừng.
    - GitHub lỗi → ghi `gh_fail_since`. Lỗi liên tục 30 phút thì alert một lần.
    - Lô gửi lại lần thứ 3 (sau 2 lần gửi lại) → `escalated` cùng alert.
    - PR `merged` → hủy các lô còn mở của PR, `stop_worker(close=True)` (session không được resume nữa), cập nhật STATE.
    - Worktree lấy từ `worktrees.list_worktrees(cfg.repo)`.
    - **`scan(..., only_pr: int | None = None, claim: bool = False)`** (G25): `only_pr` chỉ quét PR đó. `claim=True` bỏ qua gom đợt 10', tạo lô ngay, chuyển sang `delivered` với `session="manual"`, **không spawn**, in `batch_id` để session owner đang mở tự xử lý.
    - Prompt mở worker (`start_worker`) có đúng 3 nội dung: dùng skill `/pr-triage`, đường dẫn file sự kiện, và tên dự án cho CLI. **Không chứa nội dung comment.**
  - `cli.main(argv: list[str] | None = None) -> int`. Chọn dự án bằng `--project`, mặc định là dự án duy nhất có trong config dir. Các lệnh:

    | Nhóm | Lệnh |
    |---|---|
    | Owner | `init <p>`, `status`, `pending`, `backlog`, `scan [--dry-run] [--pr N [--claim]]`, `pause [PR]`, `resume [PR]`, `answer <Q> <choice> [note] [--reply TEXT] [--source URL]`, `ok <P/R> [note]`, `no <P/R> [note]`, `qa paste [--name N]` (stdin → `qa/<N>.md`), `config get <key>`, `config set <key> <value>` (chỉ `EDITABLE_KEYS`), `push-decisions <issue>`, `web` |
    | Worker | `ack <batch>`, `start <batch>` (→ `in_progress`), `item <batch> <Q/P/R> --title T --file F` (in mã), `result <batch> --file F --status done/waiting_owner`, `backlog-add <kind> <pr> <text>`, `lock acquire/release testdb --holder H`, `guard-staged [--repo .]`, `push <batch>` |
    | Thêm ở task sau (ghi ở đây để không sót) | `scan --dry-run --export-events` (Task 12), `scan --all` (Task 13), `scan --dry-run --suggest-pilot` (Task 16), allowlist qua `pause`/`resume <PR>` khi đang pause toàn bộ (Task 16), `web --demo` (Task 15) |

    - `guard-staged`: exit 1 kèm danh sách file nếu `git diff --cached --name-only` khớp `forbidden`.
    - `push <batch>`: cách **duy nhất** worker được push. Chạy trong cwd của worker. Kiểm tra lần lượt, sai ở bước nào thì exit 1 kèm lý do tiếng Việt và không push:
      1. nhánh hiện tại == `head_ref` của PR của lô;
      2. `guard-staged` sạch, và `git diff --name-only origin/<head_ref>..HEAD` không có file `forbidden`;
      3. `origin/<head_ref>` là tổ tiên của `HEAD` (không cần force).
      Sau đó chạy `["git","push","origin",f"HEAD:{head_ref}"]`, **không bao giờ `--force`**, rồi ghi timeline `pushed <sha>`.
    - `init <p>`: tạo các thư mục trong `.autopilot/`, thêm dòng `.autopilot/` vào `$(git rev-parse --git-common-dir)/info/exclude` nếu chưa có, chạy `GitHub.token()`, in hướng dẫn bật systemd ở Task 13. **Không tự bật.**

- [ ] **Step 1: Viết test fail.** Dùng `FakeGitHub`, `FakeTransport`, `FakeNotifier`.

  ```python
  # test_router.py
  def test_full_cycle_comment_to_delivery():          # comment lúc T0 → scan T0+10' → Deliver; file sự kiện tồn tại; timeline có "delivered"
  def test_dry_run_changes_nothing():                 # không gọi deliver/start; lô vẫn ở trạng thái cũ
  def test_resend_twice_then_escalate():               # lượt thoát không ack: scan 1 → resume (attempts 2), scan 2 → resume (3), scan 3 → escalated + 1 alert
  def test_auth_error_pauses_router_and_alerts():
  def test_github_down_30_min_alerts_once():
  def test_merged_pr_cancels_and_stops_worker():      # PR MERGED → lô mở thành cancelled; FakeTransport.stop_worker gọi với close=True; lô mới của PR không spawn
  def test_forbidden_file_in_pr_raises_incident():
  def test_second_scan_while_locked_exits_immediately():
  def test_worker_prompt_never_contains_comment_body():
  def test_scan_single_pr_claim_creates_batch_without_spawn():   # --pr 581 --claim → 1 lô delivered session="manual", FakeTransport không được gọi
  def test_resumes_after_limit_reset():               # limit_until = T0+30' → không giao gì lúc T0+29'; lúc T0+30' các lô waiting_limit → delivered, kv limit_until bị xóa
  # test_cli.py
  def test_init_adds_exclude_once_and_creates_dirs(tmp_git_repo):
  def test_guard_staged_blocks_forbidden(tmp_git_repo):   # stage a.png → exit 1; stage src/x.py → exit 0
  def test_push_refuses_wrong_branch_forbidden_and_non_ff(tmp_git_repo_with_remote):  # nhánh khác head_ref → 1; commit có a.png → 1; remote đi trước → 1; hợp lệ → 0, argv không có "--force"
  def test_worker_protocol_ack_start_item_result():
  def test_config_set_rejects_non_editable():
  def test_answer_command_prints_vietnamese_confirmation():
  ```

- [ ] **Step 2: Chạy test.** `uv run pytest tests/test_router.py tests/test_cli.py -v`. Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `router.py` và `cli.py`.**

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: add router scan loop and autopilot CLI"`

---

### Task 12: Skill `/pr-triage` và đánh giá chỉ đọc

**Files:**
- Create: `skills/pr-triage/SKILL.md`, `docs/eval/2026-11-12-triage-eval.md`

**Interfaces:**
- Consumes: CLI worker (Task 11), định dạng file sự kiện (Task 9), `Config.tests`, `forbidden`, `pr_rules`.

**Nội dung bắt buộc của `SKILL.md`** (dịch từ spec mục 5, không thêm luật mới):
- frontmatter `name: pr-triage` và mô tả;
- các bước khi được đánh thức (5.1), trong đó **lệnh đầu tiên luôn là `autopilot ack <batch>`**;
- **chạy tay** `/pr-triage <n>` (G25): chạy `autopilot scan --pr <n> --claim`, lấy `batch_id` in ra, rồi làm như được đánh thức;
- bảng phân loại 5.2: ngưỡng A, B, C1, C2, D-bug, D-scope, noop; quy tắc G2 (pushback ≥ 2), "Bắt buộc C2";
- thứ tự tra cứu trước khi xếp C2;
- 5.3 và 5.4 kèm lệnh cụ thể:
  - chỉ chạy lệnh trong `[tests]`;
  - `autopilot guard-staged` trước **mọi** commit;
  - push một lần, **chỉ bằng `autopilot push <batch>`**, không bao giờ `git push`;
  - reply tiếng Anh, không resolve, không ghi tên owner;
- 5.5: conflict và base đổi thì **merge**, không rebase, không đổi base. DB test thì xin `autopilot lock acquire testdb` trước;
- G7 (C2 thì im lặng), G8 (không sửa spec, chỉ `autopilot backlog-add spec-gap`), G12 (dùng `reply_hint`, diễn đạt lại bằng tiếng Anh);
- **chế độ `--eval`**: chỉ đọc, không sửa, không commit, không reply. Chỉ in bảng phân loại.
- danh sách cấm tuyệt đối: full test suite, merge, đổi base, resolve thread, tạo worktree, đụng file `forbidden`, đưa nội dung comment vào lệnh shell.

- [ ] **Step 1: Viết `SKILL.md`** theo danh sách trên. Giữ dưới 250 dòng.

- [ ] **Step 2: Viết skill coordinator cho Haiku (D10).**
  - Tạo `skills/autopilot-coordinator/SKILL.md`, dưới 80 dòng. Nội dung:
    - đọc `autopilot status` và `autopilot pending`;
    - đổi câu chat tự do của owner thành đúng lệnh `autopilot answer`, `ok`, `no`, `pause`, `resume`, `config set`;
    - **in lệnh sắp chạy rồi chờ owner xác nhận** trước khi chạy mọi lệnh làm thay đổi trạng thái;
    - không bao giờ đọc diff hay code;
    - trả lời bằng tiếng Việt.

- [ ] **Step 3: Cài hai skill.** Tạo symlink cho từng skill: `ln -s ~/.claude/scripts/autopilot/skills/<tên> ~/.claude/skills/<tên>`. Kiểm tra: `ls -l ~/.claude/skills/pr-triage/SKILL.md ~/.claude/skills/autopilot-coordinator/SKILL.md`.

- [ ] **Step 4: Chạy đánh giá chỉ đọc (spec mục 11, bước 4).**
  - Dựng file sự kiện từ comment thật của PR #579, #580, dùng `render.event_file` qua lệnh `autopilot scan --dry-run --export-events` (thêm tùy chọn này ở bước này, có test trong `tests/test_cli.py::test_export_events_writes_files_only`).
  - Chạy **một** lượt headless ở worktree tương ứng: `claude -p "/pr-triage --eval <file>" --model claude-sonnet-5-5 --permission-mode plan --output-format json` (chỉ đọc). Trước bước này phải xin phép owner, giống Task 1 Step 1.
  - So kết quả với `docs/handoff/pr-579-580-triage.md` và các quyết định owner đã đưa ra trong lịch sử.

- [ ] **Step 5: Ghi bảng kết quả** vào `docs/eval/2026-11-12-triage-eval.md`:
  - các cột: comment, phân loại của skill, phân loại đúng, ghi chú;
  - các chỉ số G10: số câu C2 bị xếp thành C1 (phải bằng 0), tỷ lệ câu hỏi thừa (≤ 20%), các mục A có vượt 3 file không.
  - **Không đạt thì sửa `SKILL.md` rồi chạy lại.** Chỉ chuyển sang Task 13 khi đạt.

- [ ] **Step 6: Commit** `git commit -m "feat: add pr-triage and coordinator skills with read-only evaluation"`

---

### Task 13: systemd, dry-run thật, diễn tập sự cố

**Files:**
- Create: `systemd/autopilot.service`, `systemd/autopilot.timer`, `systemd/autopilot-web.service`, `docs/runbook.md`
- Test: `tests/test_systemd_units.py` (đọc file unit bằng `configparser`)

**Interfaces:**
- `autopilot.service`: `Type=oneshot`, `ExecStart=%h/.claude/scripts/autopilot/.venv/bin/autopilot scan --all`. Thêm cờ `--all` vào CLI để quét mọi dự án, kèm test `test_scan_all_iterates_projects`.
- `autopilot.timer`: `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`.
- `autopilot-web.service`: `ExecStart=… autopilot web`, `Restart=on-failure`.

- [ ] **Step 1: Viết test.** `test_timer_interval_is_five_minutes`, `test_service_runs_scan_all`. Chạy, thấy FAIL, viết các unit, chạy lại thấy PASS.

- [ ] **Step 2: Viết `docs/runbook.md`** bằng tiếng Việt, dạng các lệnh copy được:
  - cài đặt (`uv sync`, `autopilot init`, symlink skill, copy unit vào `~/.config/systemd/user/`, `systemctl --user enable --now autopilot.timer autopilot-web.service`);
  - tạm dừng và chạy lại;
  - xem log;
  - khôi phục backup.

- [ ] **Step 3: Tạo `~/.config/autopilot/projects/ivt.toml`** theo spec mục 3.2. Phần `[tests]` dùng đúng các lệnh đã xác minh.
  - **Trước khi chạy `autopilot init ivt`, xin phép owner**, vì lệnh này ghi vào `.git/info/exclude` của iviettien.
  - Kiểm tra sau khi chạy: `git -C ~/Projects/MHT/iviettien status --short` không có gì mới.

- [ ] **Step 4: Dry-run trên PR thật** (spec mục 11, bước 3): chạy `autopilot scan --dry-run`. Gửi owner bảng "sẽ giao gì cho ai" và chờ owner xác nhận là đúng.

- [ ] **Step 5: Diễn tập sự cố** (spec mục 11, bước 5). Làm trên một worktree thử mới của repo autopilot (tạo như Task 1 Step 2, xóa khi xong), **xin phép owner trước**. Ghi kết quả từng tình huống vào runbook:
  1. giết tiến trình worker giữa lượt → lượt sau `--resume` lại, cùng session;
  2. `worker.claude_bin` trỏ sai đường dẫn → lượt lỗi, chạy lại 2 lần, rồi `escalated` và alert;
  3. ghi giả `limit_until` → không giao, không mở worker;
  4. stage một `a.png` → `guard-staged` chặn;
  5. tiêm sự kiện có `pushback = 2` → bắt buộc C2;
  6. vượt 5 lần đánh thức → `paused_pr`.
  7. mở tay một session `claude` trong worktree thử rồi tiêm sự kiện → lô `Queue(owner_session)`, không spawn; đóng session → lượt quét sau giao bình thường.

- [ ] **Step 6: Bật systemd.** Chỉ bật **sau khi owner đồng ý**, theo đúng lệnh trong runbook. Kiểm tra `systemctl --user list-timers | grep autopilot`.

- [ ] **Step 7: Commit** `git commit -m "feat: add systemd units and runbook"`

---

### Task 14: API web

**Files:**
- Create: `autopilot/web/__init__.py`, `autopilot/web/server.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `Store`, `Config`, `answers.*`, `set_override`, `EDITABLE_KEYS`, `list_projects`.
- Produces: `make_server(config_dir: Path, host="127.0.0.1", port=8787, token: str) -> ThreadingHTTPServer`, và `serve()` dùng cho `autopilot web`.
  - Token đọc từ `~/.config/autopilot/web-token`. Chưa có thì tự tạo bằng `secrets.token_urlsafe(32)`, quyền file `0600`.
  - Nhận token qua `?token=` (một lần, rồi đặt cookie `HttpOnly; SameSite=Strict`) hoặc header `X-Autopilot-Token`. Sai hoặc thiếu token thì trả 401.
  - Các route, đều nhận `?project=<p>`:

    | Method | Đường dẫn | Trả về hoặc tác dụng |
    |---|---|---|
    | GET | `/` | `static/index.html` |
    | GET | `/static/*` | file static |
    | GET | `/api/projects` | danh sách dự án |
    | GET | `/api/overview` | `{paused, limit_until, slots:{working,max}, open_items, incidents_24h}` |
    | GET | `/api/items` | các item mở, mỗi item có `code, kind, pr, title, body_md, created_at` |
    | GET | `/api/prs` | mỗi PR có `number, title, url, head, base, draft, review, conflict, failed_checks, batch_status, last_action, warnings[]` |
    | GET | `/api/workers` | mỗi worktree: `branch, pr, path, state, session, last_run:{started_at, ended_at, cost_usd, attempts}`. `cost_usd` đọc từ file JSON của lượt cuối (`kv run:<path>`) |
    | GET | `/api/timeline?pr=` | dòng thời gian |
    | GET | `/api/backlog` | backlog |
    | GET | `/api/settings` | giá trị hiện tại của `EDITABLE_KEYS` |
    | GET | `/api/digest` | bản tóm tắt |
    | POST | `/api/answer` | `{code, choice, note, reply_hint, source}` |
    | POST | `/api/approve` | `{code, ok, note}` |
    | POST | `/api/pause`, `/api/resume` | `{pr?}` |
    | POST | `/api/settings` | `{key, value}` |
    | POST | `/api/qa` | `{name, text}` → `qa/<name>.md` |
    | POST | `/api/push-decisions` | `{issue}` |

  - Lỗi nghiệp vụ trả 409 hoặc 404, body là JSON `{error}` bằng tiếng Việt.

- [ ] **Step 1: Viết test fail.** Server chạy ở cổng 0 trong một thread, gọi bằng `urllib`.

  ```python
  def test_requires_token():                                # không token → 401; token qua query → 200 kèm Set-Cookie
  def test_binds_localhost_only():                          # server.server_address[0] == "127.0.0.1"
  def test_answer_endpoint_resolves_and_is_idempotent():    # lần 2 → 409
  def test_settings_rejects_non_editable_key():             # 400
  def test_qa_paste_writes_file_under_qa_dir():             # name "../x" bị từ chối
  def test_overview_shapes():
  ```

- [ ] **Step 2: Chạy test.** `uv run pytest tests/test_web_api.py -v`. Mong đợi: FAIL.

- [ ] **Step 3: Cài đặt `server.py`.**

- [ ] **Step 4: Chạy lại test.** Mong đợi: pass.

- [ ] **Step 5: Commit** `git commit -m "feat: add local dashboard JSON API"`

---

### Task 15: Giao diện dashboard

**Files:**
- Create: `autopilot/web/static/index.html`, `app.js`, `style.css`

**Interfaces:** chỉ gọi các route của Task 14. Tự cập nhật mỗi 5 giây, dừng cập nhật khi tab bị ẩn.

**Yêu cầu hiển thị (spec mục 7):**
- Thanh trên cùng: chọn dự án, trạng thái router (đang chạy, tạm dừng, chờ reset lúc …), slot dạng `3/5`, nút Dừng hoặc Chạy lại.
- **"Cần bạn quyết"** luôn ở đầu trang.
  - Thẻ Q: định dạng 5 dòng, nút A và B, ô ghi chú, ô "Cách reply" (gợi ý: "Viết ý bằng tiếng Việt, worker sẽ viết lại bằng tiếng Anh"), ô link nguồn.
  - Thẻ P: tóm tắt cùng nút Duyệt hoặc Từ chối.
  - Thẻ R: nháp tiếng Anh cùng nút Gửi hoặc Sửa.
- **Bảng PR**: badge draft, review, conflict, CI. Cảnh báo xếp chồng in đậm. Nút "Copy link" (một PR) và "Copy tất cả link".
- **Worker**: thẻ cho từng worktree: nhánh, PR, trạng thái (`WorkerStatus.state` dịch tiếng Việt: chưa có session, rảnh, đang chạy, chờ bạn, lỗi, hết limit, bạn đang mở), session id rút gọn, lúc bắt đầu và kết thúc lượt cuối, chi phí lượt cuối (từ JSON), số lần chạy lại, nút copy lệnh `cd <path> && claude --resume <session>`. API `/api/workers` (Task 14) trả đủ các trường này.
- **Dòng thời gian**: lọc theo PR, có link GitHub.
- **Tóm tắt**: đêm qua và hôm nay.
- **Backlog**: tab `spec-gap` và tab `scope`.
- **Cài đặt**: form cho `EDITABLE_KEYS`, ô dán QA (textarea cùng tên), nút "Đẩy quyết định lên issue #…".
- Toàn bộ chữ tiếng Việt. Có giao diện sáng và tối theo `prefers-color-scheme`. Không cuộn ngang ở chiều rộng 390px. Nút chỉ có icon thì phải có `title` và `aria-label`. Không dùng CDN, mọi thứ inline hoặc nằm trong `static/`.

- [ ] **Step 1: Viết `index.html`, `app.js`, `style.css`.** Dùng JS thuần, không framework.

- [ ] **Step 2: Kiểm tra bằng trình duyệt.**
  - Chạy `autopilot web` với store có dữ liệu mẫu. Thêm lệnh `autopilot web --demo` để nạp dữ liệu giả vào một DB tạm, kèm test `tests/test_web_api.py::test_demo_mode_uses_temp_db`.
  - Dùng chrome-devtools MCP: `navigate_page`, rồi `take_screenshot` ở 1440×900 và 390×844.
  - Bấm thử A/B, Duyệt, Dừng và lưu một cài đặt. Kiểm tra `list_console_messages` không có lỗi.

- [ ] **Step 3: Gửi owner hai ảnh chụp** để duyệt giao diện. Ảnh lưu trong scratchpad, không commit. Sửa theo góp ý.

- [ ] **Step 4: Commit** `git commit -m "feat: add dashboard UI"`

---

### Task 16: Chạy thử thật

**Files:**
- Modify: `docs/runbook.md` (thêm mục "Kết quả chạy thử")

- [ ] **Step 1: Chọn PR.**
  - Chạy `autopilot scan --dry-run --suggest-pilot` để đề xuất 1–2 PR đang mở ít phụ thuộc nhất: không có PR con, không conflict.
  - Thêm cờ `--suggest-pilot`, kèm test `tests/test_router.py::test_suggest_pilot_prefers_leaf_prs_without_conflict`.
  - Gửi owner danh sách và chờ owner xác nhận.

- [ ] **Step 2: Bật cho đúng các PR đó.** `autopilot pause` toàn bộ, rồi `autopilot resume <PR>` cho từng PR thử. Thêm chế độ "chỉ các PR được resume": `kv allowlist`, kèm test `test_allowlist_limits_routing`.

- [ ] **Step 3: Theo dõi một ngày** trên dashboard. Ghi vào runbook:
  - số sự kiện;
  - số lô;
  - số mục A, C1, C2, D;
  - số lần owner phải can thiệp;
  - lỗi nào gặp phải.

- [ ] **Step 4: Owner quyết định mở rộng.** Nếu đồng ý, gỡ allowlist bằng `autopilot resume`.

- [ ] **Step 5: Commit** `git commit -m "docs: record pilot results"`
