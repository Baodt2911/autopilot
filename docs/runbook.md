# Autopilot runbook

Mọi lệnh bên dưới copy được. Đặt `AP=~/.claude/scripts/autopilot` cho gọn.

```bash
AP=~/.claude/scripts/autopilot
alias autopilot="$AP/.venv/bin/autopilot"
```

## 1. Cài đặt

```bash
# 1. môi trường Python (venv ở $AP/.venv)
uv sync --project $AP

# 2. cấu hình dự án: ~/.config/autopilot/projects/<tên>.toml (xem docs/specs mục 3.2)
mkdir -p ~/.config/autopilot/projects
$EDITOR ~/.config/autopilot/projects/ivt.toml

# 3. khởi tạo dự án: tạo .autopilot/, thêm vào .git/info/exclude, kiểm tra gh auth
autopilot init ivt

# 4. skill cho worker và coordinator (symlink, chỉ làm một lần)
ln -sfn $AP/skills/pr-triage ~/.claude/skills/pr-triage
ln -sfn $AP/skills/autopilot-coordinator ~/.claude/skills/autopilot-coordinator

# 5. tạm dừng router TRƯỚC khi bật timer lần đầu (chỉ quét và gom, không giao việc)
autopilot pause --project ivt

# 6. cài unit systemd (user)
mkdir -p ~/.config/systemd/user
cp $AP/systemd/autopilot.service $AP/systemd/autopilot.timer $AP/systemd/autopilot-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now autopilot.timer
# dashboard web (sau khi Task 15 xong):
systemctl --user enable --now autopilot-web.service
```

Timer: `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`. Service chạy `autopilot scan --all` (mọi dự án trong `~/.config/autopilot/projects/`).

## 2. Tạm dừng và chạy lại

```bash
autopilot pause --project ivt          # dừng toàn bộ: vẫn quét, gom sự kiện, không giao worker
autopilot pause 581 --project ivt      # chỉ dừng PR 581
autopilot resume --project ivt         # chạy lại toàn bộ
autopilot resume 581 --project ivt
autopilot status --project ivt         # dòng "Tạm dừng toàn bộ"
systemctl --user stop autopilot.timer  # tắt hẳn quét nền
systemctl --user start autopilot.timer
```

## 3. Xem log

Lưu ý: `pause` áp dụng theo từng dự án; dự án mới thêm vào bắt đầu ở trạng thái KHÔNG tạm dừng (nhớ `autopilot pause --project <tên>` trước khi timer quét lần đầu).

```bash
systemctl --user list-timers | grep autopilot
journalctl --user -u autopilot.service -n 50 --no-pager
journalctl --user -u autopilot.service --since "1 hour ago"
journalctl --user -u autopilot-web.service -n 50 --no-pager
autopilot status --project ivt
cat ~/Projects/MHT/iviettien/.autopilot/STATE.md
autopilot scan --dry-run --project ivt   # xem "sẽ giao gì cho ai" mà không ghi gì
```

## 4. Khôi phục backup

Router sao lưu mỗi ngày vào `~/.autopilot-backup/<dự án>/<ngày>/` (`state.db`, `decisions.md`, `backlog.md`, `pending.md`).

```bash
systemctl --user stop autopilot.timer
ls ~/.autopilot-backup/ivt/
DAY=2026-11-12   # chọn ngày
D=~/Projects/MHT/iviettien/.autopilot
cp $D/state.db $D/state.db.broken-$(date +%s)    # giữ bản hỏng để điều tra
cp ~/.autopilot-backup/ivt/$DAY/state.db $D/state.db
cp ~/.autopilot-backup/ivt/$DAY/decisions.md $D/decisions.md
autopilot status --project ivt
systemctl --user start autopilot.timer
```

## 5. Diễn tập sự cố


Diễn tập ngày 2026-11-12 trên một worktree thử của repo autopilot (đã xóa), CLI thật; chỉ phần GitHub là giả (repo thử không có PR) và `worker.claude_bin` trỏ tới script giả (không bao giờ chạy claude thật). Mỗi tình huống chạy `autopilot scan` thật, từng lượt một.

| # | Tình huống | Kết quả thực tế | Đạt? |
|---|------------|-----------------|------|
| 1 | Giết tiến trình worker giữa lượt (`kill -9`) | Lượt quét sau: `re-ran dead worker, batch 1, attempt 2`. Lệnh gọi lại là `--resume <cùng session id>` (lần đầu là `--session-id`, cùng id). | Đạt |
| 2 | `worker.claude_bin` sai đường dẫn | Spawn lỗi `[Errno 2] No such file`. Lần chạy đầu: lô bị thử lại vô hạn, không escalate (lỗ hổng, đã sửa). Sau sửa: 3 lần lỗi liên tiếp rồi `escalated` + alert. Khi binary tồn tại nhưng exit 1 mỗi lượt: re-run 2 lần (attempt 2, 3) rồi `escalated` + alert "worker dead after 2 re-runs" đúng như thiết kế. | Đạt sau khi sửa lưu ý A (3 lần lỗi thì escalate) |
| 3 | Ghi giả `limit_until` (3 giờ nữa) | Lô vào `queued` (`xếp hàng (paused)`), không có run record, script giả không được gọi lần nào. | Đạt |
| 4 | Stage `a.png` (`git add -f`) | `autopilot guard-staged` thoát mã 1, in `a.png`. Sau khi bỏ stage: mã 0. | Đạt |
| 5 | Sự kiện thread có `pushback = 2` | Event file có mục `Bắt buộc C2` liệt kê thread `PRRT_drill`, hai id sự kiện và `Phản bác thread PRRT_drill: 2`. | Đạt |
| 6 | Vượt 5 lần đánh thức trong ngày | Lần 1-5 giao bình thường; lần 6: `PR #581 vượt giới hạn đánh thức trong ngày: tạm dừng`, lô 6 `cancelled`, `paused_pr:581 = limit`, alert gửi; lần 7 không tạo lô. | Đạt |
| 7 | Mở tay session `claude` trong worktree rồi tiêm sự kiện | Nếu router đã từng chạy worker ở worktree đó: lô `queued` với `owner_session`, không spawn; sau khi đóng session: lượt quét sau `giao lô 2 (resume)`. | Đạt (cả worktree chưa từng dùng, sau khi sửa lưu ý B) |

Đã sửa sau diễn tập (2026-11-12), kèm test:

- Lưu ý A (lỗi spawn không leo thang): router đếm `spawn_fail:<lô>` trong kv; sau 3 lần spawn lỗi liên tiếp lô thành `escalated` kèm đúng MỘT alert, không thử lại nữa. Chạy lại drill 2: lượt 1-2 lô ở `new` (alert "không giao được" một lần), lượt 3 `escalated` + alert "bị escalate", lượt 4 không làm gì. Store cho phép `new`/`queued` -> `escalated`.
- Lưu ý B (owner_busy khi chưa có session): `status` kiểm tra tiến trình `claude` lạ trong worktree TRƯỚC khi trả `none`. Chạy lại drill 7 với worktree chưa từng dùng: lô `xếp hàng (owner_session)`, 0 lần gọi claude; đóng session của owner thì lượt sau `mở worker mới` bình thường.

## 6. Chạy thử (pilot)

Chỉ cho một vài PR được giao việc, mọi PR khác giữ tạm dừng (vẫn quét, ghi sự kiện, hiển thị).

```bash
# 1. Đề xuất 1-2 PR an toàn nhất (chỉ đọc): có worktree, không conflict, không PR con, ít file nhất, không file cấm
autopilot scan --dry-run --suggest-pilot --project ivt

# 2. Tạm dừng toàn bộ, rồi bật từng PR thử (resume <PR> khi đang tạm dừng = thêm vào allowlist)
autopilot pause --project ivt
autopilot resume 581 --project ivt
autopilot resume 589 --project ivt

# Gỡ một PR khỏi allowlist
autopilot pause 589 --project ivt

# 3. Theo dõi trên dashboard: thanh trên cùng hiện "Chạy thử: #581, #589"

# 4. Mở rộng toàn bộ (xóa allowlist và bỏ tạm dừng)
autopilot resume --project ivt
```

Allowlist lưu ở kv `allowlist` (danh sách số PR dạng JSON). Chỉ có tác dụng khi router đang tạm dừng toàn bộ và allowlist không rỗng.

### Kết quả chạy thử

(Controller điền sau khi theo dõi một ngày: số sự kiện, số lô, số mục A/C1/C2/D, số lần owner can thiệp, lỗi gặp phải.)

### Test bằng một tài khoản

Đặt `self_review_prefix = "[review]"` trong file cấu hình của project test. Khi đó comment của chính tác giả PR mà bắt đầu bằng `[review]` được tính như comment của người review. Reply của worker không có tiền tố này nên vẫn được coi là trả lời của tác giả, không tạo vòng lặp. Không đặt tuỳ chọn này cho project thật.
