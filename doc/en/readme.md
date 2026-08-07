# Cloud Uploader

Upload any file to the cloud and share a download link, entirely from the keyboard. No account or API key needed.

## Usage

- **NVDA+alt+o**: choose a file from disk, or record a new clip (microphone, computer audio, or both), then pick an upload host and expiry.
- **NVDA+alt+l**: browse your upload history. Enter opens Copy/Open/Delete options; Control+C copies the link directly; Delete removes an entry. Expired links drop off the list automatically.
- **NVDA+shift+c**: start a background recording with no window (uses your default source and devices). Press again to stop and open the usual record dialog to preview, edit, and upload.

### Recording

The record dialog lets you capture the microphone, computer audio, or both together, with Preview, Undo/Redo, silence removal, noise reduction, volume normalizing, and (with both sources) separate volume sliders. Recordings are encoded to MP3, WAV, or FLAC (via ffmpeg) before upload, based on your Settings. Use NVDA+shift+c for a headless start/stop toggle that ends in the same dialog.

## Upload hosts

Files are uploaded anonymously to whichever host you pick:

| Host | Link type | Retention |
|---|---|---|
| Litterbox (catbox.moe) | Direct download | 1 hour–3 days, your choice |
| Gofile | Download page | ~10 days |
| Catbox (catbox.moe) | Direct download | Permanent |
| 0x0.st | Direct download | 30 days–1 year, depending on size |
| Filebin | Download page | ~6 days |
| Uguu | Direct download | ~48 hours |

## Terms of service and acceptable use

**Every host listed above is a free, independently-operated third-party service, not something this add-on runs itself.** Each has its own rules on file size, allowed content, and what happens if those rules are broken. This add-on does not check file content or enforce these rules for you — it is your responsibility to follow each host's terms. A summary, as of 2026:

- **Litterbox / Catbox** (catbox.moe): Catbox caps files at 200 MB; Litterbox (temporary) allows up to 1 GB. Both disallow `.exe`, `.scr`, `.cpl`, `.doc*`, and `.jar` files, and ban child sexual abuse material, malware, full pirated TV/anime episodes, and heavy gore. Commercial use (e.g. as a CDN or ecommerce image host) requires prior approval. Violations result in the file being deleted and **your IP address being blacklisted** from the service.
- **Gofile**: No officially published per-file size limit, but free accounts have a traffic allowance (historically around 100 GB/month) and are rate-limited per endpoint — exceeding limits can return errors or trigger a **temporary IP block**. Free-tier files are generally kept around 10 days unless downloaded; content that violates their terms may be removed and accounts restricted.
- **0x0.st**: 512 MiB max file size. Its terms explicitly prohibit piracy, pornography/gore, extremist or terrorist material, malware or botnet infrastructure, doxxing or personal-data dumps, AI-generated spam ("AI slop"), automated mass uploads, and anything illegal under German law (the host's jurisdiction). Violations get the file removed and **may block your IP** from further uploads.
- **Filebin**: No fixed per-file size cap, but the service has an overall storage capacity limit and will reject new uploads when it's full. IP addresses are logged for abuse handling and **may be shared with law enforcement on request**; IPs found uploading malicious content are blocked. Content is not automatically moderated, but is expected to comply with the terms (no illegal, copyrighted, or malicious material).
- **Uguu**: 128 MiB max file size on the official instance, with a short automatic expiry (a few hours to a few days). Malware is explicitly disallowed. Copyright takedowns go through abuse@pomf.se.

**In short:** stick to reasonable, legal content and reasonable file sizes, and don't rely on any of these hosts for anything sensitive, permanent, or high-volume. If a host blocks your IP for a terms violation, that block is enforced by the host itself — this add-on has no way to appeal it or work around it for you.

## Settings

Available under NVDA+control+g → Cloud Uploader: a default host, auto-copy on upload, history size, and recording options (format, quality, device, channels, auto-start recording, ffmpeg path).

## Notes

- Only one upload runs at a time.

## Support

If Cloud Uploader has been useful to you, a "Donate to support development"
button is available in Settings, or you can go directly to
[ko-fi.com/naday](https://ko-fi.com/naday).
