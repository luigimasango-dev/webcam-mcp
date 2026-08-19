# webcam-mcp

MCP server giving Claude eyes and ears on this machine: webcam stills and
microphone speech-to-text. Built 2026-08-01 to close a capability gap — there was
no camera or audio input path in the session at all.

**Everything runs locally.** ffmpeg captures, faster-whisper transcribes on CPU.
No audio, video, or transcript is sent anywhere. The only network call the server
ever makes is a one-time model download from Hugging Face on first transcription.

## Tools

| Tool | What it does |
|---|---|
| `list_devices` | Enumerate cameras and microphones. Call this first when a capture fails. |
| `capture_frame` | One still JPEG from a camera. Returns a path — open it with Read to actually view it. |
| `record_audio` | Record a mic to 16kHz mono WAV. No transcription. |
| `transcribe_file` | Transcribe any existing audio/video file to text. |
| `record_and_transcribe` | Record the mic and return what was said. Blocks for the duration. |
| `capture_stitched` | Both cameras in one image — first on top, second below. |
| `list_voices` | Text-to-speech voices installed on this machine. |
| `speak` | Say something out loud, or render it to a WAV. |
| `listen_for_wake_word` | Wait for "hey Jarvis", then record and transcribe what follows. |

## Devices on this machine

- **HD WebCam** — built-in, the default camera
- **Luigi's A16 (Windows Virtual Camera)** — phone-as-webcam, only live when the phone app is running
- **Microphone Array (Realtek(R) Audio)** — the default mic

## Notes earned the hard way

- **`mcp` is pinned `<2`.** mcp 2.0.0 renames `mcp.server.fastmcp` to
  `mcp.server.mcpserver`, which breaks the import silently at server start. Every
  other MCP server on this machine is on 1.28.1 — the suite stays on one version.
- **`warmup_seconds` is not padding.** Webcams open dark and auto-exposure needs a
  moment. Below ~1s the frame comes back black or badly exposed. Default is 1.5s.
- **Capture failures are almost always a device already in use.** Zoom, Teams, or a
  Chrome tab holding the camera produces an ffmpeg I/O error. The tools detect this
  and say so. Second most common cause is Windows privacy settings blocking desktop
  apps (Settings > Privacy & security > Camera / Microphone).
- **Whisper model sizes:** `base` (default) is the speed/accuracy sweet spot on a
  16GB CPU-only laptop. `small` is meaningfully better on strong accents at roughly
  3x the compute. Models cache under the user profile after first download.
- **Models load lazily and stay cached** per size for the life of the server
  process — reloading per call costs seconds and a lot of RAM.

## Voice, wake word, and their limits

`listen_for_wake_word` uses openWakeWord's pretrained **`hey_jarvis`** model — no
training, no dataset, it ships in the box. Verified at 0.999 confidence on the wake
phrase and 0.000 on an unrelated sentence, so false positives are not a concern at
the default 0.5 threshold.

`speak` goes through the legacy SAPI voices (David, Zira). They sound robotic.
Windows 11's natural voices are much better and run locally once installed:
**Settings > Accessibility > Narrator > Add natural voices**. `speak` picks them up
automatically once they exist.

**The honest architectural limit:** this is a four-hop pipeline — speech to text,
text to model, model to text, text to speech. It is slower than a native audio
model, and converting speech to text discards tone, volume, and emotion before the
model ever sees the words. A single-hop native-audio setup would fix both, but
Claude does not accept audio natively, so that would mean a different provider
running outside Claude Code entirely. Within this harness, four hops is the
correct design, not a shortcut.

`capture_stitched` only earns its keep when the phone-as-webcam app is actually
running. Otherwise the bottom half is a connection spinner.

## Output files

Captures default to `%TEMP%\claude-webcam\` with timestamped names. Pass
`output_path` to put them somewhere durable. `record_and_transcribe` deletes its
WAV afterwards unless `keep_audio=True`.

Nothing here prunes old captures automatically — clear that folder occasionally.

## Selftest

```
.venv\Scripts\python.exe server.py --selftest
```

Prints the device list without needing an MCP client.
