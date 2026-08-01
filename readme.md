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

## Settings

Available under NVDA+control+g → Cloud Uploader: a default host, auto-copy on upload, history size, and recording options (format, quality, device, channels, auto-start recording, ffmpeg path).

## Notes

- Only one upload runs at a time.
