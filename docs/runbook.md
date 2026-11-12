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

```bash
systemctl --user list-timers | grep autopilot
journalctl --user -u autopilot.service -n 50 --no-pager
journalctl --user -u autopilot.service --since "1 hour ago"
journalctl --user -u autopilot-web.service -n 50 --no-pager
tail -n 50 ~/Projects/MHT/iviettien/.autopilot/router.log
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
| 2 | `worker.claude_bin` sai đường dẫn | Spawn lỗi `[Errno 2] No such file`, lô ở lại `new`, bị thử lại ở MỖI lượt quét (đã chạy 4 lượt, vẫn `new`), chỉ có MỘT thông báo "Lô 1 không giao được" (chống lặp). KHÔNG tự `escalated`. Khi binary tồn tại nhưng exit 1 mỗi lượt: re-run 2 lần (attempt 2, 3) rồi `escalated` + alert "worker dead after 2 re-runs" đúng như thiết kế. | Một phần, xem lưu ý A |
| 3 | Ghi giả `limit_until` (3 giờ nữa) | Lô vào `queued` (`xếp hàng (paused)`), không có run record, script giả không được gọi lần nào. | Đạt |
| 4 | Stage `a.png` (`git add -f`) | `autopilot guard-staged` thoát mã 1, in `a.png`. Sau khi bỏ stage: mã 0. | Đạt |
| 5 | Sự kiện thread có `pushback = 2` | Event file có mục `Bắt buộc C2` liệt kê thread `PRRT_drill`, hai id sự kiện và `Phản bác thread PRRT_drill: 2`. | Đạt |
| 6 | Vượt 5 lần đánh thức trong ngày | Lần 1-5 giao bình thường; lần 6: `PR #581 vượt giới hạn đánh thức trong ngày: tạm dừng`, lô 6 `cancelled`, `paused_pr:581 = limit`, alert gửi; lần 7 không tạo lô. | Đạt |
| 7 | Mở tay session `claude` trong worktree rồi tiêm sự kiện | Nếu router đã từng chạy worker ở worktree đó: lô `queued` với `owner_session`, không spawn; sau khi đóng session: lượt quét sau `giao lô 2 (resume)`. | Đạt, xem lưu ý B |

Lưu ý A (lỗ hổng, chưa sửa): lỗi spawn (binary không tồn tại) không bao giờ leo thang. Lô bị thử lại vô hạn mỗi 5 phút, owner chỉ nhận một thông báo duy nhất. Sửa đề xuất: đếm số lần spawn lỗi theo lô trong kv và `escalated` sau 2 lần.

Lưu ý B (lỗ hổng, chưa sửa): phát hiện `owner_busy` chỉ chạy khi router đã có session lưu cho worktree (`status` trả `none` trước khi kiểm tra tiến trình). Nếu owner đã mở `claude` ở một worktree mà router chưa từng chạy worker ở đó, lô đầu tiên vẫn `Start` một worker mới song song với session của owner (đã tái hiện). Từ lô thứ hai trở đi thì phát hiện đúng.
