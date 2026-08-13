# Telegram Link — Manual Acceptance

Real-Telegram checks that automated tests cannot cover. **Never record QR payloads,
tokens, login codes, Telegram 2FA passwords, raw sessions, or encryption material here.**
Record only the state sequence and the outcome.

## Part 1 — QR failure classification (Task 1, one real attempt)

Run one fresh QR attempt and record exactly one classification:

```text
Telegram app → Settings → Devices → Link Desktop Device → scan the Turntable QR
```

| Class | Meaning | Observed state sequence (log only) |
| --- | --- | --- |
| A | Scanner cannot recognize the QR at all | |
| B | Telegram recognizes the QR but reports invalid/expired | |
| C | Telegram accepts the QR but Turntable never reaches ready/password_required | |
| D | Turntable reaches ready but the browser stays on the login gate | |

- [ ] Attempt performed on: ________
- [ ] Classification recorded: A / B / C / D
- [ ] Backend state sequence noted (flow states only, no token material)

## Part 2 — Release acceptance gate (Task 14)

All real-scan steps below are **user-verified only** — an automated agent cannot
perform them and must not claim them.

- [ ] Fresh desktop QR login: scan → Turntable linked → library opens, no manual refresh, no phone fallback, no error dialog
- [ ] Fresh QR login repeated 3 times (one success is not enough): 1) ☐ 2) ☐ 3) ☐
- [ ] QR expiry + automatic regeneration: let it expire, observe textual expiry/regeneration, scan the new QR, login completes
- [ ] QR + Telegram 2FA (if account has it): scan → password stage → correct password → library opens
- [ ] Phone login with local-format number (e.g. Iran + `0912…`): backend receives `+98912…`, code arrives, login completes
- [ ] Phone login with pasted international number (`+98 912 …`): country/prefix normalize, no duplicated code, login completes
- [ ] Country replacement: remembered country shown → Backspace once → field empty → type another country → choose it → prefix updates
- [ ] Expected errors inline: wrong code, wrong 2FA password, invalid phone → stage stays visible, friendly inline message, global modal stays closed
- [ ] Responsive: desktop = QR left / phone right; mobile = phone first / QR second; no horizontal overflow; no `Recommended` badge unless every QR step above passed
