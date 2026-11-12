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

(Ghi kết quả ở mục dưới sau khi diễn tập.)
