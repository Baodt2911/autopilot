# Spike transport headless (Task 1)

Ngày chạy: 2026-11-12. Phiên bản: `claude` 2.1.322. Model: `claude-sonnet-5-5`.
Owner đã đồng ý chạy spike (owner approved via /goal). Spike chạy trong worktree thử
`../autopilot-spike` (nhánh `spike-transport`) và một worktree thử thứ hai ngoài `~/.claude/`
(xem phát hiện 4.1). Cả hai đã xóa ở cuối. Không đụng Orca, không đụng iviettien (chỉ đọc `/proc`).

## Kết luận nhanh

**Cổng dừng: PASS.** `--resume` giữ ngữ cảnh. Có tổ hợp cờ cho worker sửa file, chạy lệnh, `git add`, `git commit`
mà không treo. `git push` và `rm` bị chặn bằng `--disallowedTools`, tiến trình không treo.

Ba điều bất ngờ, ảnh hưởng thẳng tới Task 8:

1. **`is_error` vẫn `false` khi lệnh bị từ chối quyền.** Exit code 0, `subtype: "success"`. Chỉ `permission_denials` (mảng không rỗng) cho biết lượt kẹt. Task 8 phải coi `permission_denials` có mục ngoài `disallowed_tools` là lượt chưa hoàn thành.
2. **Hook RTK của owner chạy ở `-p` và viết lại lệnh** (`git add x.txt` thành `rtk git add x.txt`). Luật `Bash(git add:*)` không khớp lệnh đã viết lại, nên lượt bị kẹt. Phải cho phép cả dạng `rtk ...` (hoặc tắt hook cho worker).
3. **Ghi file dưới `~/.claude/` bị coi là vị trí nhạy cảm** và bị chặn ở `acceptEdits`. Worktree thật nằm ở `~/Projects/MHT/iviettien/.worktrees/...` nên không bị, chỉ là hiện tượng của worktree thử đặt trong `~/.claude/scripts/`.

## Bước 3: lượt đầu và lượt resume

```
SID=6dbb9fdf-7dcf-440e-b533-07772b5ff9a6
claude -p "Reply with exactly: ACK t1" --session-id $SID --model claude-sonnet-5-5 --output-format json > t1.json
claude -p "What token did I ask you to reply with before? Reply with it only." --resume $SID --model claude-sonnet-5-5 --output-format json > t2.json
```

Exit code cả hai: `0`. `session_id` của t2 **bằng** `$SID`. t2 **không** trả đúng "ACK t1": trường `result` của t2 là chính xác `"t1"` (không có "ACK"). Đây là lệch so với yêu cầu của brief; ngữ cảnh vẫn được giữ (model biết token liên quan tới lượt trước), và được xác nhận chắc hơn bằng mã `PELICAN-42` bên dưới. Tiêu chí "giữ ngữ cảnh" nên dựa vào phép thử mã `PELICAN-42`, không phải khớp chính xác chuỗi.

JSON thô của t1 và t2 (rút gọn còn các khóa `type`, `subtype`, `is_error`, `session_id`, `result`, `total_cost_usd`, `permission_denials`, `num_turns`):

```json
t1: {"type":"result","subtype":"success","is_error":false,"session_id":"6dbb9fdf-7dcf-440e-b533-07772b5ff9a6","result":"ACK t1","total_cost_usd":0.08890150000000001,"permission_denials":[],"num_turns":1}
t2: {"type":"result","subtype":"success","is_error":false,"session_id":"6dbb9fdf-7dcf-440e-b533-07772b5ff9a6","result":"t1","total_cost_usd":0.1647499,"permission_denials":[],"num_turns":1}
```
Để chắc chắn, ở worktree thứ hai tôi nhờ nhớ mã `PELICAN-42` ở lượt 1, các lượt `--resume` sau (kể cả sau khi kill, bước 5) đều trả đúng `PELICAN-42`. Ngữ cảnh được giữ.

Trường JSON của t1 (kiểu `result`, đủ bộ):

| Trường | Ghi chú |
|---|---|
| `type` = `"result"`, `subtype` = `"success"` | |
| `is_error` | `false` (xem bất ngờ 1) |
| `result` | văn bản trả lời cuối: `"ACK t1"` |
| `session_id` | uuid, khớp `--session-id` |
| `uuid` | id của dòng kết quả (khác session) |
| `stop_reason` | `"end_turn"` |
| `terminal_reason` | `"completed"` |
| `num_turns` | 1 |
| `duration_ms`, `duration_api_ms`, `ttft_ms`, `time_to_request_ms`, ... | thời gian |
| `total_cost_usd` | 0.0889 (t1), 0.1647 (t2; chi phí của riêng lượt đó, cache 1h được tạo lại) |
| `usage` | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `iterations`, ... |
| `modelUsage` | theo model, có `costUSD`, `contextWindow` |
| `permission_denials` | mảng `{tool_name, tool_use_id, tool_input}` |
| `api_error_status` | `null` khi ổn |
| `safety_stops`, `subagent_stats`, `fast_mode_state`, `queued_turn_count`, `result_index`, `credential_expires_at` | khác |

`--model sonnet` (bí danh) không thử; config vẫn dùng id đầy đủ `claude-sonnet-5-5`.

Transcript: `~/.claude/projects/-home-mht--claude-scripts-autopilot-spike/6dbb9fdf-7dcf-440e-b533-07772b5ff9a6.jsonl`.
Tên thư mục = cwd tuyệt đối với mọi `/` **và** `.` đổi thành `-`
(`/home/mht/.claude/scripts/autopilot-spike` thành `-home-mht--claude-scripts-autopilot-spike`). Tên file đúng là `$SID.jsonl`, và `--resume` ghi tiếp vào cùng file.

## Bước 4: quyền khi không có người duyệt

Mọi lệnh có `</dev/null` (không treo chờ stdin). Chạy ở worktree thử thứ hai (ngoài `~/.claude/`).

| Lần | Cờ | Kết quả |
|---|---|---|
| 4.0 (worktree trong `~/.claude/`) | `--permission-mode acceptEdits --allowedTools "Bash(git add:*)" "Bash(git commit:*)" "Bash(echo:*)" Edit Write Read` | **Kẹt mềm**: `Write x.txt` bị chặn (đường dẫn dưới `~/.claude/`), `permission_denials` có Write. Exit 0, `is_error:false`. |
| 4.1 (worktree ngoài `~/.claude/`) | như 4.0 | `Write` qua, `echo` qua, **`git add` bị chặn** vì hook RTK viết lại thành `rtk git add x.txt`. Không commit được. |
| 4.2 | `--permission-mode auto` + cùng `--allowedTools` | **Chạy trọn**: tạo file, add, echo, commit. `DONE`, 7 lượt, `permission_denials: []`. |
| 4.3 | `--permission-mode acceptEdits --allowedTools "Bash(git add:*)" "Bash(git commit:*)" "Bash(echo:*)" "Bash(rtk git add:*)" "Bash(rtk git commit:*)" "Bash(rtk echo:*)" "Bash(autopilot:*)" "Bash(rtk autopilot:*)" Edit Write Read` | **Chạy trọn**: commit `spike2` tạo ra, `permission_denials: []`, 5 lượt. Đây là tổ hợp hẹp nhất. |
| 4.4 (chặn) | `--permission-mode auto --disallowedTools "Bash(git push:*)" "Bash(rm:*)" "Bash(rtk git push:*)" "Bash(rtk rm:*)"`, yêu cầu `git push /tmp/nonexistent-remote HEAD` và `rm -rf /tmp/x` | Cả hai bị từ chối, `permission_denials` có 2 mục, tiến trình thoát bình thường, exit 0. (Dùng đích push giả để an toàn.) |
| 4.5 (vòng sửa 1: bảng đầy đủ dưới `acceptEdits`) | `--permission-mode acceptEdits --allowedTools Edit Write Read "Bash(git add:*)" "Bash(git commit:*)" "Bash(git status:*)" "Bash(git diff:*)" "Bash(echo:*)" "Bash(autopilot:*)" "Bash(pnpm test:*)"` + toàn bộ dạng `rtk` tương ứng, `--disallowedTools "Bash(git push:*)" "Bash(rtk git push:*)" "Bash(rm:*)" "Bash(rtk rm:*)"`; yêu cầu (a) `git status`, (b) tạo `w.txt`, `git add`, `git commit`, (c) `git push /tmp/nonexistent-remote HEAD`, (d) `rm z.txt` | **Không treo** (6,9 giây, exit 0). (a) chạy, (b) commit `9ee2de0` tạo ra, (c) và (d) bị từ chối, `z.txt` còn nguyên. `permission_denials` có đúng 2 mục (`rtk git push /tmp/nonexistent-remote HEAD`, `rm z.txt`). |

Ghi chú:

- Tổ hợp hẹp nhất vừa đủ là **4.3**: `acceptEdits` + allowlist, thêm dạng `rtk` vì hook của owner. `auto` cũng chạy nhưng rộng hơn (để classifier tự quyết), không cần cho worker; giữ làm phương án dự phòng.
- 4.5 đã chạy riêng ở `acceptEdits` với bảng đầy đủ: luật cấm áp dụng ở chế độ này (xem JSON dưới). Lưu ý: ở 4.5 `permission_denials` không rỗng **do luật cấm cố ý chặn**, tức không phải lượt kẹt. Router phân biệt bằng nội dung: mục bị từ chối khớp `disallowed_tools` (git push, rm) là bình thường; mục khác (ví dụ lệnh trong danh sách cho phép nhưng bị chặn do thiếu dạng `rtk`) là kẹt.

JSON thô của 4.5 (rút gọn):

```json
{"type":"result","subtype":"success","is_error":false,"session_id":"70d2b6ca-1f29-4c59-b4e6-8dbb6ebad9e4","result":"(a) git status: ran. (b) commit: worked, 9ee2de0 \"spike3\". (c) git push: denied by the permission system. (d) rm z.txt: denied, z.txt still there.","total_cost_usd":0.0915148,"permission_denials":[{"tool_name":"Bash","tool_use_id":"toolu_019xCaDmBAG4d1ZcSpS5nwfp","tool_input":{"command":"rtk git push /tmp/nonexistent-remote HEAD"}},{"tool_name":"Bash","tool_use_id":"toolu_01JGgrHaRpmqcFochBbkxTSM","tool_input":{"command":"rm z.txt"}}],"num_turns":5}
```
- Chế độ `auto` có classifier riêng (lệnh `git reset --hard` của chính tôi trong công cụ Bash của phiên điều khiển từng bị chặn bởi classifier kiểu này; không phải lượt `claude -p`). Worker dùng `acceptEdits` thì không có lớp đó, vì vậy `disallowed_tools` phải đủ chặt.
- `Bash(autopilot:*)` đã nằm trong allowlist nhưng spike không có lệnh `autopilot` thật để chạy. Chưa kiểm chứng việc gọi nó, chỉ kiểm chứng cú pháp luật.
- **Hook**: `~/.claude/settings.json` có hook `SessionStart`, `UserPromptSubmit` (Orca `claude-hook.cmd`, `codegraph prompt-hook`) và (suy ra từ việc lệnh bị viết lại) hook RTK trước `Bash`. Ở `-p`, các hook **đều chạy** (RTK viết lại lệnh, thấy trong `permission_denials[].tool_input.command`). Không hook nào làm hỏng stdout JSON: `t1.json` đến `t5b.json` đều parse được bằng `json.load`.

## Bước 5: chạy nền và phát hiện tiến trình (G24)

```
setsid timeout 300 claude -p "Run the bash command 'sleep 90' then reply DONE." --resume $SID2 ... --output-format json > t5.json </dev/null &
```

Khi lượt đang chạy, `/proc/*/cmdline` có chứa `claude` cho ra:

- `timeout 300 claude -p ...` (pid 278821, pgid 278821) là tiến trình đầu nhóm, `setsid` tạo ra.
- `claude -p ...` (pid 278823, ppid 278821, **cùng pgid 278821**).
- `node -e ...` (con của claude, hook/helper) và `zsh -c ...` (con chạy lệnh Bash của lượt), cùng nhóm hoặc nhóm con.
- cwd của tất cả: đúng thư mục worktree (`readlink /proc/<pid>/cwd`).

Phân biệt tiến trình router tạo với tiến trình khác: router lưu **pgid** (trả từ `setsid`/`Popen(start_new_session=True)`, pgid = pid con). Mọi tiến trình có pgid đó là của router. Tiến trình `claude` có cwd trong worktree mà pgid khác là của người khác.

Khi không có lượt nào, các tiến trình `claude` sẵn có của owner (chỉ đọc):

```
2089039 cwd=/home/mht/Projects/MHT/iviettien/.worktrees/int-a2  claude --plugin-dir ~/.config/orca/claude-agent-view/<hash>/orca-agent-view --dangerously-skip-permissions ...
2100343 cwd=/home/mht/Projects/MHT/iviettien/.worktrees/int-d   claude --plugin-dir ... --dangerously-skip-permissions ...
(còn int-c2, int-e, int-b2, int-b3 và một session ở thư mục gốc iviettien: cùng dạng; chỉ đọc, không đụng tới)
```

Kết luận cho câu 5 của spec mục 14: **đọc `/proc/<pid>/cwd` nhận ra được** session owner (kể cả mở trong Orca, cwd là đúng thư mục worktree). Lọc nhiễu:

- Chỉ lấy tiến trình có `argv[0]` có basename `claude` (tách `cmdline` theo NUL). Bỏ `node -e ...` (con helper, cwd trùng worktree nhưng không phải session), `zsh -c`, `bun`/`uv` của claude-mem.
- Bỏ claude-mem observer: `argv[0]=/home/mht/.local/bin/claude --output-format stream-json --input-format stream-json ...` với cwd `~/.claude-mem/observer-sessions/N` (không nằm trong worktree nên vô hại, nhưng nếu so cwd theo tiền tố thì tự loại).
- Khớp cwd: `cwd == worktree` hoặc `cwd.startswith(worktree + "/")`. **Chú ý** `iviettien` (gốc) là tiền tố của mọi `.worktrees/*`; nếu worktree là thư mục gốc thì phải so chính xác hoặc nhớ rằng `.worktrees/` nằm trong gốc. Với layout thật (`.worktrees/<tên>`), so tiền tố theo từng worktree là đúng, nhưng worktree gốc không được tính các con của nó.
- Một worktree có thể có nhiều tiến trình owner (ví dụ `int-a2` có 2). Chỉ cần ít nhất một.
- Loại pgid của router: không tính tiến trình có pgid trùng pgid mà router biết.
- Hai tiến trình `claude` (2088811, 2089028) cùng cwd gốc iviettien: bình thường.

Kill nhóm: `kill -TERM -278821` (chỉ nhóm tôi tạo). Sau 3 giây không còn tiến trình trong nhóm, không còn `sleep 90`. `t5.json` **rỗng** (không có JSON khi bị kill), `t5.err` rỗng. Lượt `--resume` kế tiếp chạy bình thường, trả `PELICAN-42`, cùng `session_id`. Tức kill giữa lượt không làm hỏng session; nhưng lượt đó mất kết quả, nên router phải coi stdout rỗng hoặc không parse được là "lượt chưa có result" và chạy lại (đúng G17).

## Bước 6: dấu hiệu hết usage limit

Chỉ đọc transcript cũ. `grep -F "hit your"` trong `~/.claude/projects/` có ở nhiều file `.jsonl` (cả transcript chính và subagent), thí dụ
`-home-mht-Projects-MHT-iviettien/da9fb04c-e48f-4922-95f7-370e93483a56.jsonl`. Dòng mẫu (rút gọn):

```json
{"type":"assistant","message":{"model":"<synthetic>","role":"assistant","content":[{"type":"text","text":"You've hit your 5-hour limit · resets 6:30pm (Asia/Bangkok)"}]},"error":"rate_limit","isApiErrorMessage":true}
```

Định dạng: `You've hit your <5-hour|...> limit · resets <h:mmam/pm> (<IANA timezone>)`, trong dòng `assistant` với `model:"<synthetic>"`, `error:"rate_limit"`, `isApiErrorMessage:true`. (Ví dụ spec/plan: `resets 7:20pm (Asia/Bangkok)`.)

**Chưa kiểm chứng** nó hiện ra ở `result` của JSON, ở stderr, hay chỉ trong transcript khi chạy `-p` (không giả lập được). Task 8 vì vậy quét cả ba: trường `result`, stderr và file transcript của session (dòng cuối có `isApiErrorMessage:true` và `error:"rate_limit"`). Gợi ý thêm: khi hết limit thì `is_error` có thể là `true` hoặc `api_error_status` khác `null`; chưa thấy, cần coi là tín hiệu thứ tư.

## Bước 7: dọn dẹp

Xóa `../autopilot-spike` và `spike-transport`, cùng worktree thử thứ hai và nhánh `spike-transport-b`. Kết quả thực tế sau khi dọn:

```
$ git branch -D spike-transport spike-transport-b
Deleted branch spike-transport (was b443605).
Deleted branch spike-transport-b (was bf12f18).
$ git worktree list
/home/mht/.claude/scripts/autopilot  b443605 [feat/phase-1-2]
$ git branch --list 'spike*'
(trống)
```

Vòng sửa 1 dùng thêm worktree thử thứ ba (`spike-transport-c`, ngoài `~/.claude/`), đã xóa cùng nhánh; `git worktree list` sau đó chỉ còn `/home/mht/.claude/scripts/autopilot  c95d61d [feat/phase-1-2]`, `git branch --list 'spike*'` trống.

## Lệnh chốt cho Task 8

| Mục | Giá trị |
|---|---|
| `start_worker` (argv, cwd = worktree) | `["claude","-p",<chỉ thị>,"--session-id",<uuid>,"--model","claude-sonnet-5-5","--output-format","json","--permission-mode",<permission_mode>,"--allowedTools",*<allowed_tools>,"--disallowedTools",*<disallowed_tools>]` |
| `deliver` (argv, cwd = worktree) | `["claude","-p",<chỉ thị>,"--resume",<uuid>,"--model","claude-sonnet-5-5","--output-format","json","--permission-mode",<permission_mode>,"--allowedTools",*<allowed_tools>,"--disallowedTools",*<disallowed_tools>]` |
| Cách chạy | `subprocess.Popen(argv, cwd=worktree, stdin=DEVNULL, stdout=<file t.json>, stderr=<file t.err>, start_new_session=True)`; bọc `timeout 300`-tương đương trong router (ví dụ `["timeout","300",*argv]`) hoặc tự canh giờ; lưu pgid = pid. Luôn `stdin=DEVNULL`. |
| `[worker] permission_mode` | `acceptEdits` |
| `[worker] allowed_tools` | Đã kiểm chứng dưới `acceptEdits` (4.3 và 4.5): `Edit`, `Write`, `Read`, `Bash(rtk git add:*)`, `Bash(rtk git commit:*)`, `Bash(rtk git status:*)` (4.5), `Bash(rtk echo:*)` (4.3). `Bash(git add:*)`, `Bash(git commit:*)`, `Bash(echo:*)` (dạng không `rtk`) đã khớp ở 4.2 dưới `auto`; dưới `acceptEdits` dạng không `rtk` không khớp vì hook viết lại lệnh. **Chưa kiểm chứng (có trong allowlist 4.3/4.5 nhưng không có lệnh nào dùng tới):** `Bash(git status:*)`, `Bash(git diff:*)`, `Bash(rtk git diff:*)`, `Bash(autopilot:*)`, `Bash(rtk autopilot:*)` (chỉ kiểm tra cú pháp luật, không có lệnh `autopilot` thật), `Bash(pnpm test:*)`, `Bash(rtk pnpm test:*)` (chưa kiểm chứng; lệnh test thật của dự án chưa biết). Cần thêm các dạng `rtk` tương ứng cho mọi mục vì hook của owner. |
| `[worker] disallowed_tools` | `Bash(git push:*)`, `Bash(rtk git push:*)`, `Bash(rm:*)`, `Bash(rtk rm:*)` (thêm `Bash(git reset --hard:*)`, `Bash(git push --force:*)` tùy chọn). `git push` luôn trong danh sách này; push chỉ qua `autopilot push`. |
| Dự phòng nếu `acceptEdits` vẫn kẹt | `--permission-mode auto`. Đã kiểm chứng chạy trọn ở 4.2 với allowlist **nhỏ hơn bảng này**: `Edit Write Read "Bash(git add:*)" "Bash(git commit:*)" "Bash(echo:*)"` (không có `rtk`, `autopilot`, `status`, `diff`, `pnpm test`). Với allowlist đầy đủ ở `auto` thì chưa chạy riêng.
| Đọc kết quả | `json.load(stdout)`. Lượt **thành công** khi: parse được, `type=="result"`, `subtype=="success"`, `is_error==false`, không có mục `permission_denials` nào ngoài các mục khớp `disallowed_tools`, `result` không rỗng. Lỗi khi `is_error==true`, hoặc `subtype!="success"`, hoặc `api_error_status!=null`. **Lượt kẹt** (cần chạy lại / báo): `permission_denials` có mục KHÔNG khớp `disallowed_tools` dù `is_error==false` (mục khớp luật cấm là bình thường, xem 4.5). stdout rỗng hoặc không parse được (bị kill, crash) là "chưa có result". |
| Trường session id | `session_id` (luôn bằng uuid truyền qua `--session-id` / `--resume`). Văn bản trả lời: `result`. Chi phí: `total_cost_usd`. |
| Tìm transcript | `~/.claude/projects/<cwd>/<session_id>.jsonl`, với `<cwd>` = đường dẫn tuyệt đối của worktree (đã `realpath`) thay mọi `/` và `.` bằng `-`. Thí dụ `/home/mht/Projects/MHT/iviettien/.worktrees/int-a2` thành `-home-mht-Projects-MHT-iviettien--worktrees-int-a2`. Ghi chú: cách mã hóa đã kiểm chứng trên spike và khớp các thư mục hiện có trong `~/.claude/projects/`. |
| Phát hiện tiến trình `claude` khác trong worktree | Quét `/proc/[0-9]*`: lấy `argv0 = cmdline.split("\0")[0]`, giữ khi `basename(argv0)=="claude"`; `cwd = readlink(/proc/pid/cwd)`; giữ khi `cwd == wt` hoặc `cwd.startswith(wt + "/")`; loại pid có pgid là pgid router đang giữ (hoặc pid con của router); loại claude-mem observer (argv chứa `--input-format stream-json`). Kết quả không rỗng: `owner_busy`. Không bao giờ gửi tín hiệu tới các tiến trình này. |
| Dừng một lượt treo | `os.killpg(pgid, SIGTERM)` (chỉ pgid do router tạo); sau đó `--resume` vẫn dùng được. Stdout sẽ rỗng. |
| Hết usage limit | Quét `result`, stderr và dòng cuối transcript, regex `hit your .*limit.*resets (\d{1,2}(:\d{2})?\s*[ap]m) \(([^)]+)\)`. Chưa kiểm chứng nguồn nào xuất hiện ở `-p`. |

Số lượt `claude -p` đã chạy: 11 (t1, t2, t3, t3a, t3b, t4a, t4b, t4c, t5 [bị kill], t5b, t6 ở vòng sửa 1), tất cả Sonnet.
