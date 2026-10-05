# Cloud Uploader

Upload any file to the cloud and share a download link, entirely from the keyboard. No account or API key needed.

## Usage

Everything starts from one shortcut: **NVDA+alt+o**. It opens a menu with:

- **&Upload a file** — choose a file from disk, then pick an upload host and expiry.
- **&Record** — record a new clip (microphone, computer audio, or both), then pick an upload host and expiry.
- **&Background recording** — starts a headless recording with no window (uses your default source and devices). Press NVDA+alt+o again (no need to open the menu) to stop it and open the usual record dialog to preview, edit, and upload.
- **&History** — browse your upload history. Enter opens Copy/Open/Delete options; Control+C copies the link directly; Delete removes an entry. Expired links drop off the list automatically.

Each menu item's underlined letter (U, R, B, H) can be pressed directly once the menu is open, the same as any other Windows menu. Escape closes the menu without doing anything.

Only one shortcut is assigned by default, to avoid clashing with NVDA's own commands or other add-ons. Every menu item can also be reached as its own separately-assignable command from NVDA's Input Gestures dialog (NVDA+N → Preferences → Input gestures → Cloud Uploader) — including "Record" on its own, for a direct recording shortcut without opening the menu first.

Each of "Record," "Background recording," and "History" can be hidden from the NVDA+alt+o menu individually in Settings, for example if you've given it its own shortcut and don't need it cluttering the menu, or just don't use it. If all three are hidden, NVDA+alt+o skips the menu entirely and goes straight to choosing a file to upload.

### Recording

The record dialog lets you capture the microphone, computer audio, or both together, with Preview, Undo/Redo, silence removal, noise reduction, volume normalizing, and (with both sources) separate volume sliders. Recordings are encoded to MP3, WAV, or FLAC (via ffmpeg) before upload, based on your Settings.

## Upload hosts

Files are uploaded anonymously to whichever host you pick:

| Host | Link type | Retention |
|---|---|---|
| Litterbox (catbox.moe) | Direct download | 1 hour–3 days, your choice |
| Gofile | Download page | ~10 days |
| Catbox (catbox.moe) | Direct download | Permanent |
| Filebin | Download page | ~6 days |
| Uguu | Direct download | ~48 hours |
| Buzzheavier | Download page (not a direct link) | 15 days, +3 days per download, up to 45 |
| x0.at | Direct download | 3–100 days, depending on file size |

## Terms of service and acceptable use

**Every host listed above is a free, independently-operated third-party service, not something this add-on runs itself.** Each has its own rules on file size, allowed content, and what happens if those rules are broken. This add-on does not check file content or enforce these rules for you — it is your responsibility to follow each host's terms. A summary, as of 2026:

- **Litterbox / Catbox** (catbox.moe): Catbox caps files at 200 MB; Litterbox (temporary) allows up to 1 GB. Both disallow `.exe`, `.scr`, `.cpl`, `.doc*`, and `.jar` files, and ban child sexual abuse material, malware, full pirated TV/anime episodes, and heavy gore. Commercial use (e.g. as a CDN or ecommerce image host) requires prior approval. Violations result in the file being deleted and **your IP address being blacklisted** from the service.
- **Gofile**: No officially published per-file size limit, but free accounts have a traffic allowance (historically around 100 GB/month) and are rate-limited per endpoint — exceeding limits can return errors or trigger a **temporary IP block**. Free-tier files are generally kept around 10 days unless downloaded; content that violates their terms may be removed and accounts restricted.
- **Filebin**: No fixed per-file size cap, but the service has an overall storage capacity limit and will reject new uploads when it's full. IP addresses are logged for abuse handling and **may be shared with law enforcement on request**; IPs found uploading malicious content are blocked. Content is not automatically moderated, but is expected to comply with the terms (no illegal, copyrighted, or malicious material).
- **Uguu**: 128 MiB max file size on the official instance, with a short automatic expiry (a few hours to a few days). Malware is explicitly disallowed. Copyright takedowns go through abuse@pomf.se.
- **Buzzheavier**: no fixed file size limit, and it keeps your original file name. Free files start at 15 days and each download adds 3 days (up to 45). The link Cloud Uploader gives you opens a download page, which has the direct link on it. **x0.at**: 1 GiB max file size, direct link, keeps your file name (cleaned up), and keeps files 3 to 100 days depending on their size. Both are free, independently-operated services with their own rules against abuse; violations can get files removed and **your IP blocked**. There is no way to choose how long a file is kept on either of them.

**In short:** stick to reasonable, legal content and reasonable file sizes, and don't rely on any of these hosts for anything sensitive, permanent, or high-volume. If a host blocks your IP for a terms violation, that block is enforced by the host itself — this add-on has no way to appeal it or work around it for you.

A short summary of this notice is shown, in a dialog, the first time NVDA starts after installing this add-on. You must check "I have read and understand the above" before Agree becomes available. Choosing Disagree, or dismissing the dialog with Escape, does not record acceptance, so it will be shown again the next time NVDA starts. It will not be shown again after that unless the wording of this notice is meaningfully updated in a future version — everyday updates (new features, bug fixes) won't bring it back.

## Settings

Available under NVDA+control+g → Cloud Uploader: a default host, auto-copy on upload, history size, recording options (format, quality, device, channels, auto-start recording, ffmpeg path), which items show up in the NVDA+alt+o menu, and a "View debug log" button showing Cloud Uploader's own recent log lines - useful for troubleshooting or reporting a bug, without digging through NVDA's full log.

## Notes

- Only one upload runs at a time.
- Uploading, viewing history, and background recording are all disabled until you've agreed to the terms of service notice above. If you dismiss it without agreeing, restart NVDA to see it again.
- All shortcuts can be reassigned from NVDA's Input Gestures dialog (NVDA+N → Preferences → Input gestures → Cloud Uploader).

## Support

If Cloud Uploader has been useful to you, a "Donate to support development"
button is available in Settings, or you can go directly to
[ko-fi.com/naday](https://ko-fi.com/naday).

## Contact

Questions, bug reports, or feature requests: dianarl0206@gmail.com
