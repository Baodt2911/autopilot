# Autopilot: tự động xử lý comment PR và điều phối worker

- Ngày: 2026-11-12 (bản 2, sau buổi grilling)
- Trạng thái: **Đã duyệt (owner, 2026-11-12)**
- Dự án áp dụng đầu tiên: `iviettien`. Dự án khác dùng chung code, chỉ cần thêm một file cấu hình.

## 1. Mục tiêu

Owner chỉ trả lời những vấn đề **thực sự ảnh hưởng tới dự án**. Owner không phải gõ prompt liên tục để hỏi "xong chưa", "đã push chưa".

### Tiêu chí thành công

1. Comment mới trên PR được giao tới đúng worktree trong vòng ≤ 15 phút (5 phút quét + 10 phút gom đợt). Owner không phải gõ gì.
2. Không có câu hỏi nào gửi tới owner mà spec, QA hoặc `decisions.md` đã có đáp án. Mỗi câu C2 ghi rõ đã tra ở đâu.
3. Một câu owner đã trả lời thì không bao giờ bị hỏi lại, ở bất kỳ worktree nào.
4. Không mất sự kiện khi session chết, context bị nén, Orca tắt hoặc máy khởi động lại.
5. Không tốn token khi không có việc.
6. Không file nào của autopilot lọt vào commit của dự án.

### Không thuộc phạm vi

- Tự merge PR, tự đổi base, tự resolve thread.
- Tự sửa spec của dự án.
- Chạy full test suite.
- Chạy trên cloud.

## 2. Quyết định đã chốt

### 2.1 Thiết kế ban đầu

| # | Quyết định |
|---|---|
| D1 | Mục **A** (sửa nhỏ) và **C1** (câu hỏi đã có đáp án): tự commit, push và reply bằng tiếng Anh. Mục **B** (sửa lớn): viết plan rồi chờ owner `ok`. |
| D2 | Mọi việc cần owner đều về **một nơi**: Dashboard web (chính), CLI `autopilot`, hoặc Haiku (tùy chọn). |
| D3 | Quét mỗi **5 phút, 24/7**. Có việc là xử lý ngay. |
| D4 | Router là script, làm phần chắc chắn và không dùng LLM. Claude chỉ làm phần cần phán đoán. |
| D5 | Gom đợt **10 phút**. Ack trong **15 phút**, gửi lại tối đa 2 lần. Tối đa **5 worker** đang chạy. |
| D6 | "Nhỏ" (A) nghĩa là **≤ 3 file**, không đụng schema, migration, hợp đồng API hay layout UI, và không ảnh hưởng PR khác. |
| D7 | **Không tự resolve thread.** |
| D8 | Dữ liệu nằm trong `<repo>/.autopilot/`, ignore qua `.git/info/exclude`. Không đụng file nào mà git đang theo dõi. |
| D9 | Code dùng chung. Cấu hình riêng mỗi dự án nằm trong `.toml`. |
| D10 | Không có coordinator LLM chạy thường trực. Một session **Haiku** dùng chung, chỉ chạy khi owner mở nó. |
| D11 | Worker dùng **Sonnet**. Mỗi worktree một session, chỉ đóng khi PR merge. Không đóng session đang bị usage limit. |
| D12 | Dashboard web làm ở **giai đoạn 2**. |

### 2.2 Chốt thêm sau grilling

| # | Quyết định |
|---|---|
| G1 | Reply dưới tài khoản `vyn-bao-do`, **không** ghi chú "tự động". |
| G2 | Reviewer phản bác **lần thứ hai** trên cùng một thread thì tự chuyển thành **C2**. Worker không tranh luận tiếp. |
| G3 | "Không phải lỗi" **bắt buộc phải reply** cho reviewer. Worker viết nháp (lý do + bằng chứng `file:dòng`), **owner duyệt** rồi mới gửi. "Ngoài phạm vi, sẽ làm ở PR khác" thì tự reply. |
| G4 | Theo dõi **mọi PR do `vyn-bao-do` tạo**, gồm cả draft. Xử lý **mọi comment**, kể cả của bot. Chỉ bỏ qua comment thuần "LGTM / approved": không reply, nhưng vẫn ghi vào dòng thời gian. |
| G5 | PR xếp chồng: khi PR cha push, router tạo sự kiện "base đã cập nhật" cho PR con. Worker của PR con **merge base vào** (không rebase). Merge sạch thì push; có conflict chạm logic thì xếp B. |
| G6 | Worker đang chờ owner **không chiếm slot**. Chỉ worker đang code hoặc đang chạy test mới tính. |
| G7 | Thread có câu C2 đang chờ: **im lặng** cho tới khi owner quyết. |
| G8 | **Không bao giờ sửa spec của dự án**, trừ khi owner yêu cầu. Chỗ hổng spec chỉ ghi vào `backlog.md` với nhãn `spec-gap` và hiện trên web. |
| G9 | Giới hạn: mỗi thread tối đa **3 vòng** reply, quá thì chuyển C2. Mỗi PR tối đa **5 lần đánh thức mỗi ngày**, quá thì `pause` PR và báo owner. Chỉnh được trong `.toml`, và trên web ở giai đoạn 2. |
| G10 | Điều kiện bật tự push: **0** lần worker tự quyết một câu lẽ ra phải hỏi owner; câu hỏi thừa **≤ 20%**; mọi mục A đúng ngưỡng ≤ 3 file. |
| G11 | Thông báo desktop chỉ trong khung **7h–22h**. Việc phát sinh ban đêm dồn vào bản tóm tắt 8h sáng. Câu chưa trả lời sau **4 giờ** (trong khung giờ) thì nhắc một lần. Chỉnh được. |
| G12 | Reply sau khi owner quyết C2: **không ghi tên owner**. Có mẫu mặc định chỉnh được. Owner có thể viết ý bằng tiếng Việt, worker diễn đạt lại bằng tiếng Anh. Có link nguồn nếu owner đưa. |
| G13 | Không tự đẩy quyết định lên issue. Web có nút để owner bấm khi muốn. |
| G14 | **Chỉ đổi base khi owner yêu cầu.** Khi GitHub tự đổi base của PR con, xử lý như G5. |
| G15 | Router **báo** (không tự sửa) khi: (a) nhánh cha vừa merge, kèm danh sách PR con bị ảnh hưởng, base hiện tại và tình trạng conflict; (b) có PR merge sai thứ tự hoặc vào sai nhánh đích. |
| G16 | Chỉ owner mới trả lời được C2, duyệt plan B và duyệt nháp "không phải lỗi". Duyệt PR là việc của reviewer (approve rồi merge). Comment của reviewer không bao giờ được tính là duyệt. |
| G17 | **Bỏ bộ chờ nền và Stop hook của worker** ở giai đoạn 1. Dựa vào việc Orca tự gõ thông báo vào terminal, cộng với ack và gửi lại của router. Chỉ thêm lại nếu chạy thử thấy lỡ tin. |
| G18 | Worker mở bằng `orca orchestration worker-start --worktree … --agent claude --model <id đầy đủ> --keep-open`. Tin gửi qua `send --to dispatch:<id>`, **không** gõ thẳng vào terminal. Cách cụ thể chốt sau khi test ở bước đầu của plan. |
| G19 | Usage limit tính theo **tài khoản**. Phát hiện ở một worker thì **dừng giao việc cho tất cả**, không coi worker là chết, chờ tới giờ reset + 5 phút. Web hiện "Đang chờ reset lúc …". |
| G20 | QA là tùy chọn. Owner **dán QA từ Google Sheet** trên web (giai đoạn 1: `autopilot qa paste`). Router không truy cập Google. |
| G21 | DB test của API: chỉ bật khi sửa chạm tới `*.int-spec.ts` liên quan. **Mỗi lần chỉ 1 worker** dùng (router giữ khóa). Xong lô thì tắt. |
| G22 | Transport thay thế được: `orca` (app hoặc `orca serve`, không cần giao diện) ở giai đoạn 1; `headless` (`claude -p --resume`) ở giai đoạn 3. |
| G23 | Chạy thử thật trên 1–2 PR ít phụ thuộc nhất. Router đề xuất, owner xác nhận. |

## 3. Kiến trúc

```
systemd user timer (5') ─► autopilot router (Python, 0 token)
                            ├─ quét GitHub (gh): comment, review, merge, conflict, CI, base đổi
                            ├─ quét Orca: worker sống / chết / đứng im / hết limit
                            ├─ chống trùng, gom đợt, xếp hàng ưu tiên, giới hạn G9
                            ├─ định tuyến PR → nhánh → worktree → session
                            ├─ ghi events/*.md, gửi tin trỏ tới file (transport)
                            └─ thông báo desktop theo khung giờ G11
                                         │
                                         ▼
           Worker Claude Sonnet trong worktree: skill /pr-triage
                                         │ C2 / plan B / nháp "không phải lỗi"
                                         ▼
        .autopilot/pending.md ─► Web / CLI / Haiku ─► owner trả lời
                                         │
                                         ▼
          decisions.md ─► router chuyển nguyên văn câu trả lời cho worker
```

### 3.1 Vị trí

```
~/.claude/scripts/autopilot/        code: dự án công cụ cá nhân (xem mục 13)
  autopilot/                        package Python: router, cli, store, github, transport/, dashboard
  skills/pr-triage/SKILL.md         cài vào ~/.claude/skills/
  systemd/                          autopilot.service, autopilot.timer
  tests/                            pytest + dữ liệu gh đã ghi lại
  docs/specs/                       tài liệu này

~/.config/autopilot/projects/<dự án>.toml   cấu hình máy, không nằm trong repo nào

<repo>/.autopilot/                  dữ liệu dự án, ignore qua .git/info/exclude
  state.db  events/  plans/  qa/  decisions.md  pending.md  backlog.md  STATE.md  screenshots/  router.log

~/.autopilot-backup/<dự án>/<ngày>/   sao lưu state.db và decisions.md mỗi ngày
```

- Mọi worktree dùng đường dẫn tuyệt đối tới `.autopilot/` của checkout chính.
- `.git/info/exclude` nằm trong thư mục `.git` dùng chung, nên có hiệu lực cho mọi worktree.

### 3.2 Cấu hình dự án

```toml
repo           = "~/Projects/MHT/iviettien"
author         = "vyn-bao-do"
gh_user        = "vyn-bao-do"   # router luôn dùng token của đúng tài khoản này (mục 13)
transport      = "orca"
scan_minutes   = 5
debounce_min   = 10
max_workers    = 5
worker_model   = "claude-sonnet-5-5"
spec_branch    = "feature/cms-05-meeting-spec-qa-update"
spec_sources   = ["docs/use_cases/", "docs/use_cases/cms-module-coverage.md", ".autopilot/qa/"]
forbidden      = [".autopilot/", "docs/superpowers/", "docs/handoff/", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp"]

[limits]
thread_rounds    = 3
wakes_per_pr_day = 5

[notify]
hours            = "07:00-22:00"
remind_after_h   = 4
digest_at        = "08:00"

[reply]
decision_template = "Updated per product decision: {summary}. {source}"

[pr_rules]
lang      = "en"
max_files = 40
template  = ".github/PULL_REQUEST_TEMPLATE.md"
git_style = ".claude/rules/git-style.md"

[tests]   # không bao giờ chạy full suite; tránh pnpm --filter web test và pnpm check:changed
web_file  = "cd apps/web && pnpm exec node ../../scripts/heavy-lock.mjs vitest run --maxWorkers=2 {file}"
api_unit  = "cd apps/api && pnpm exec jest --selectProjects unit {file}"
api_int   = "cd apps/api && pnpm exec node ../../scripts/heavy-lock.mjs jest --selectProjects integration --maxWorkers=2 {file}"
test_db_up   = "cd apps/api && pnpm test:db:up"
lint      = "pnpm exec eslint {files}"        # chạy trong apps/web hoặc apps/api
typecheck = { web = "pnpm --filter web typecheck", api = "pnpm --filter api typecheck" }
```

Lệnh `autopilot init <dự án>` làm 4 việc: tạo `.autopilot/`, thêm vào `.git/info/exclude`, kiểm tra `gh auth`, bật timer.

## 4. Router

### 4.1 Một lượt quét

1. **Khóa**: lock file, để hai lượt quét không bao giờ chồng nhau.
2. **GitHub** (`gh api graphql`): lấy mọi PR đang mở của `author`, gồm cả draft, với các thông tin:
   - review thread chưa resolve;
   - review và comment thường;
   - `mergeable = CONFLICTING`;
   - CI fail;
   - **base đã đổi**;
   - PR đã merge hoặc đóng từ lượt trước.
3. **Lọc**:
   - bỏ comment của `author`;
   - bỏ thread mà comment cuối là reply của `author`;
   - bỏ sự kiện đã thấy;
   - comment thuần "LGTM / approved" ghi `noop` (G4).
4. **ID sự kiện**: node id của comment/review; `conflict:<pr>:<headSha>`; `ci:<pr>:<headSha>:<check>`; `base:<pr>:<baseSha>`; `merged:<pr>`.
5. **Gom đợt**: giao khi PR không có comment mới trong `debounce_min` phút, hoặc khi reviewer submit review.
6. **Giới hạn** (G9): đếm số vòng mỗi thread và số lần đánh thức mỗi PR trong ngày.
7. **Định tuyến** (4.3) và gửi tin.
8. **Theo dõi worker** (4.4).
9. **Hậu kiểm**: diff PR có đường dẫn `forbidden` thì báo sự cố. Phát hiện nhánh cha merge hoặc merge sai thứ tự thì báo (G15).
10. Sinh lại `STATE.md`. Sao lưu mỗi ngày.

### 4.2 Trạng thái sự kiện

```
new ─► batched ─► delivered ─► acked ─► in_progress ─► done
                     │                       ├─► waiting_owner   (C2 / plan B / nháp "không phải lỗi")
                     │ 15' chưa ack          ├─► waiting_limit   (G19)
                     ▼                       └─► lease 60' hết ─► giao lại
                 gửi lại (≤2) ─► escalated
noop:       comment LGTM / approved
paused:     vượt giới hạn G9, hoặc owner pause
cancelled:  PR đóng hoặc merge giữa chừng
```

### 4.3 Định tuyến

- Chuỗi tra cứu: PR → `headRefName` → worktree (khớp `git worktree list` với `orca worktree ps`) → session của worktree đó.
- Có session: gửi qua transport. Nội dung chỉ là đường dẫn tới file sự kiện.
- Chưa có session và đang dưới `max_workers` worker chạy: mở worker ở **đúng worktree đó** (G18). Đã đủ thì xếp hàng.
- Ưu tiên: `CHANGES_REQUESTED` > conflict > base đổi > CI fail > comment thường.
- PR không có worktree local: báo sự cố, không tự tạo worktree.

### 4.4 Theo dõi worker

- Nguồn dữ liệu:
  - `orca worktree ps --agent-wait --json`: `state`, `programStatus.state`, `hasLivePty`, `lastAssistantMessage`, `updatedAt`;
  - `orca terminal read`;
  - `orca orchestration worker-list`.
- **Chết**: mở lại ở cùng worktree, giao lại sự kiện chưa xong. Chết 2 lần thì báo sự cố.
- **Đứng im** 60 phút mà còn sự kiện: nhắc một lần, sau đó báo sự cố.
- **Hết limit** (G19): phát hiện dòng "hit your … limit · resets HH:MM" trong output hoặc transcript, rồi dừng giao việc cho tất cả tới giờ reset + 5 phút.
- **PR merge**: cập nhật `STATE.md`, đóng session của worktree, rồi kiểm tra G15.

### 4.5 Transport

Giao diện chung: `deliver(event)`, `start_worker(worktree)`, `status(worktree)`, `stop_worker(worktree)`.

- `orca` (giai đoạn 1): như G18. Cần app Orca hoặc `orca serve`.
- `headless` (giai đoạn 3): `claude -p --resume <session-id>` chạy trong worktree, mỗi sự kiện một lượt.

## 5. Worker: skill `/pr-triage`

### 5.1 Khi được đánh thức

1. Đọc file sự kiện, ghi ack.
2. Đọc `decisions.md`.
3. `git fetch`, kiểm tra nhánh khớp remote.
4. Chỉ làm trong phạm vi PR này.

### 5.2 Phân loại

| Loại | Tiêu chí | Hành động |
|---|---|---|
| **A** | Theo D6 | Sửa luôn |
| **B** | Không thỏa A, hoặc đổi hành vi so với spec, hoặc ảnh hưởng nhánh xếp chồng | Viết plan vào `plans/`, chờ owner duyệt |
| **C1** | Câu hỏi hoặc đề xuất, tra được đáp án | Trả lời kèm trích dẫn `file:dòng`. Cần sửa thì làm như A hoặc B |
| **C2** | Không có trong spec, hoặc mâu thuẫn, hoặc reviewer yêu cầu PO xác nhận, hoặc reviewer phản bác lần 2 (G2), hoặc quá 3 vòng (G9) | Ghi vào `pending.md`. **Im lặng trên thread** (G7) |
| **D-bug** | Xác minh thấy không phải lỗi | Viết nháp reply, chờ owner duyệt rồi **bắt buộc gửi** (G3) |
| **D-scope** | Ngoài phạm vi PR | Tự reply. Ghi vào `backlog.md` |
| **noop** | Thuần LGTM / approved | Không làm gì (G4) |

- **Thứ tự tra cứu trước khi xếp C2**: `decisions.md` → `spec_sources` trên `spec_branch` → `.autopilot/qa/` → spec cũ trong git history → hành vi hiện có trong code.
- Phát hiện spec có chỗ hổng thì ghi `spec-gap` vào `backlog.md`, **không sửa spec** (G8).

### 5.3 Xác minh trước khi sửa

- Lỗi: viết test fail trước, hoặc có bằng chứng `file:dòng`.
- Không tái hiện được: xếp D-bug.

### 5.4 Thứ tự làm trong một lô

1. Gửi ngay các mục C2, plan B và nháp D-bug lên `pending.md`.
2. Làm A và C1:
   - sửa tối thiểu;
   - chỉ chạy test của file thay đổi và test trực tiếp của module (lệnh trong `[tests]`);
   - eslint trên file thay đổi;
   - typecheck tối đa một lần;
   - commit theo `git_style`.
3. Trước khi commit: không có file nào thuộc `forbidden` được stage.
4. Push một lần. Reply từng thread bằng tiếng Anh, kèm SHA hoặc trích dẫn. Không resolve thread.
5. Owner trả lời xong: làm tiếp, reply theo G12, rồi push.
6. Ghi `events/<id>.result.md` (≤ 5 dòng).

### 5.5 Conflict, base đổi, CI, push

- **Conflict hoặc base đổi**: merge base vào nhánh PR (không rebase, không đổi base). Conflict chạm logic thì xếp B.
- **CI fail**: chạy riêng test bị fail, sửa như A hoặc B.
- **Push bị từ chối**: fetch, merge, push lại 1 lần. Vẫn lỗi thì xếp B.
- **Test liên quan fail sau khi sửa**: không push, xếp B.
- **DB test** (G21): xin khóa từ router, xong lô thì tắt.

## 6. Hỏi đáp với owner

### 6.1 Định dạng

```
[Q-ivt-12] PR #589 · <link thread>
Reviewer hỏi: "<trích nguyên văn>"
Spec: <file:dòng> nói "…"  |  hoặc: không đề cập (đã tra: decisions, use_cases, QA, spec cũ, code)
Vì sao không tự quyết: <mâu thuẫn / reviewer yêu cầu PO / phản bác lần 2>
A: … (đề xuất · ảnh hưởng) | B: … (ảnh hưởng)
```

- Plan B có mã `P-ivt-<n>`: tóm tắt ≤ 10 dòng, kèm đường dẫn tới plan đầy đủ.
- Nháp D-bug có mã `R-ivt-<n>`: nội dung reply bằng tiếng Anh và bằng chứng.

### 6.2 Trả lời

| Kênh | Ví dụ |
|---|---|
| Web (giai đoạn 2) | nút A/B, ô ghi chú, ô "Cách reply", nút Duyệt / Từ chối |
| CLI | `autopilot answer Q12 B "giữ quyền admin" --reply "ý reply tiếng Việt"` · `autopilot ok P5` · `autopilot ok R3` · `autopilot no P5 "…"` |
| Haiku (tùy chọn) | chat tự do; Haiku gọi lệnh CLI tương ứng |

- Router ghi `decisions.md` (Q, câu trả lời, ngày, nguồn), rồi chuyển nguyên văn cho worker.
- Câu trả lời mơ hồ thì worker hỏi lại bằng một câu C2 mới.

### 6.3 Thông báo

- `notify-send` cho: C2, plan B, nháp D-bug, sự cố, cảnh báo G15. Chỉ gửi trong khung giờ G11.
- Tóm tắt lúc 8h sáng: đêm qua làm được gì, việc nào đang chờ owner, PR nào sẵn sàng, `spec-gap` mới phát hiện.

## 7. Dashboard (giai đoạn 2)

- Chạy ở `http://127.0.0.1:<port>`, có token. Mở được trong trình duyệt của Orca.
- Đọc `state.db`, không tốn token.
- Các khu vực:
  - **Cần bạn quyết**: C2, P, R;
  - **Bảng PR**: nhánh, base, review, conflict/CI, sự kiện, thứ tự merge, cảnh báo xếp chồng;
  - **Worker**: trạng thái, slot đang dùng / 5, "Đang chờ reset lúc …";
  - **Dòng thời gian**: có cả mục noop;
  - **Tóm tắt**;
  - **Backlog**: `spec-gap`, D-scope;
  - **Cài đặt**: giới hạn G9, khung giờ G11, mẫu reply, **dán QA**;
  - **Điều khiển**: pause / resume, copy link PR, đẩy quyết định lên issue (G13);
  - **Chọn dự án**.
- Mọi nút dùng chung logic với CLI. Cài đặt ghi ngược vào `.toml`.

## 8. CLI

```
autopilot init <dự án>       autopilot status [dự án]      autopilot pending      autopilot backlog
autopilot answer <Q> <lựa chọn> ["ghi chú"] [--reply "ý"]   autopilot ok|no <P|R> ["ghi chú"]
autopilot scan [--dry-run]   autopilot pause|resume [PR]   autopilot qa paste     autopilot config get|set <khóa> [giá trị]
```

## 9. Xử lý lỗi

| Sự cố | Xử lý |
|---|---|
| GitHub hoặc mạng lỗi, bị rate limit | Bỏ qua lượt này, thử lại với thời gian chờ tăng dần. Lỗi liên tục 30 phút thì báo |
| `gh` hết phiên đăng nhập | Báo ngay kèm lệnh cần chạy. Router tạm dừng |
| Transport lỗi | Sự kiện ở trạng thái `delivered`, chưa ack thì gửi lại. Thất bại 3 lần thì báo |
| Worker chết, đứng im, hết limit | Theo 4.4 và G19 |
| Đủ 5 worker | Xếp hàng theo 4.3 |
| Comment mới tới khi worker đang làm lô khác | Đưa vào lô sau |
| Vượt giới hạn G9 | `paused` và báo |
| PR đóng hoặc merge giữa chừng | `cancelled` |
| Có file `forbidden` | Chặn khi commit. Lọt lên PR thì báo |
| Máy khởi động lại | systemd chạy lại, tiếp tục từ `state.db` |
| `git clean -fdx` xóa `.autopilot/` | Khôi phục từ `~/.autopilot-backup/` |

## 10. Bảo mật

- Dữ liệu chỉ đi tới GitHub (qua `gh` của owner) và Anthropic (qua Claude Code).
- Dashboard chỉ nghe ở `127.0.0.1`, có token.
- Nội dung comment là dữ liệu, không bao giờ được chạy như lệnh.

## 11. Test và triển khai

1. **Spike transport**:
   - kiểm tra `worker-start` ở worktree có sẵn;
   - `send --to dispatch:` tới session đang rảnh và đang bận;
   - `orca serve` khi không mở app;
   - phát hiện usage limit.
   Kết quả quyết định G17 và G18.
2. **Unit test router**: lọc, chống trùng, gom đợt, định tuyến, vòng trạng thái, gửi lại, giới hạn G9, G15.
3. **Dry-run** trên các PR thật.
4. **Đánh giá triage** ở chế độ chỉ đọc, trên PR cũ (ví dụ `docs/handoff/pr-579-580-triage.md`), theo tiêu chí G10.
5. **Diễn tập sự cố**: giết worker, tắt Orca, giả lập hết limit, stage một ảnh, phản bác lần 2, vượt giới hạn.
6. **Chạy thử thật** theo G23, rồi mở rộng.

## 12. Các giai đoạn

| Giai đoạn | Nội dung |
|---|---|
| 1 | Router, CLI, `state.db`, `/pr-triage`, transport `orca`, thông báo, systemd. Test bước 1–6 |
| 2 | Dashboard web, gồm phần cài đặt và dán QA |
| 3 | Transport `headless`. Thêm dự án thứ hai bằng một file `.toml` |

Owner (2026-11-12): **một plan triển khai bao gồm cả giai đoạn 1 và 2**. Web được làm ngay sau khi router chạy đúng, và phải xong trước khi chạy thử thật (mục 11, bước 6), để owner theo dõi trực quan trên web ngay từ lần chạy thật đầu tiên.

## 13. Bản chất dự án và danh tính git

- **Autopilot là một dự án công cụ cá nhân**, độc lập với mọi dự án công ty. Code ở `~/.claude/scripts/autopilot/`.
- Cấu hình máy (`~/.config/autopilot/`) và dữ liệu từng dự án (`<repo>/.autopilot/`) **không thuộc** repo này.
- Commit của repo autopilot dùng **danh tính git cá nhân** của owner, không dùng `vyn-bao-do`.
- Remote (nếu có) do owner tự tạo. Không tự tạo repo, không tự push.
- Khi chạy, autopilot thao tác trên PR của dự án công ty bằng tài khoản `gh_user` (iviettien: `vyn-bao-do`). Mọi lệnh `gh` được chạy với `GH_TOKEN=$(gh auth token --user <gh_user>)`, nên **dù tài khoản đang active trên máy là ai**, PR công ty vẫn được xử lý dưới đúng tài khoản. Nếu không lấy được token của `gh_user`, router dừng và báo owner.
- Commit và push trên worktree của dự án dùng danh tính git của chính repo đó (iviettien: `vyn-bao-do`, qua SSH host `github.com`).
- Repo autopilot dùng danh tính cá nhân, tự áp dụng qua `includeIf "gitdir:~/.claude/scripts/"`, và remote qua SSH host `github-personal`. Hai danh tính không bao giờ lẫn nhau.

## 14. Còn cần kiểm chứng (ở bước 1 của plan)

1. `--model` có nhận tên viết tắt hay phải dùng id đầy đủ.
2. Tin gửi qua `send --to dispatch:<id>` có tới được session mở bằng `--keep-open` lúc đang rảnh và lúc đang bận không.
3. `orca serve` có hỗ trợ terminal và agent như app đầy đủ không.
4. Dòng báo usage limit có xuất hiện trong `lastAssistantMessage` hoặc `terminal read` không.
