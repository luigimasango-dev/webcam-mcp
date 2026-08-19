# webcam-mcp

An MCP server that gives a coding agent eyes and ears on the host machine:
webcam stills, microphone speech-to-text, and optional text-to-speech replies.

Everything runs locally. ffmpeg captures frames, faster-whisper transcribes on
CPU, openWakeWord handles the wake word. No audio, video, transcript, or image is
sent anywhere. The only network call the server ever makes is a one-time model
download from Hugging Face the first time you transcribe something.

Why it exists: a coding agent that can read your screen but cannot see you or
hear you is missing a whole side of a conversation. This closes that gap for
any MCP client on a machine with a camera and a microphone.

## Tools

| Tool | What it does |
|---|---|
| `list_devices` | Enumerate cameras and microphones. Call this first when a capture fails. |
| `capture_frame` | One still JPEG from a camera. Returns a file path. |
| `record_audio` | Record a mic to 16kHz mono WAV. No transcription. |
| `transcribe_file` | Transcribe any existing audio/video file to text. |
| `record_and_transcribe` | Record the mic and return what was said. Blocks for the duration. |
| `capture_stitched` | Two cameras in one image, first on top, second below. |
| `list_voices` | Text-to-speech voices installed on the machine. |
| `speak` | Say something out loud, or render it to a WAV. |
| `listen_for_wake_word` | Wait for a wake phrase, then record and transcribe what follows. |

## Setup

Requires Python 3.11+, ffmpeg on PATH, and uv.

```
uv sync
```

Run standalone to confirm it starts:

```
uv run server.py
```

It sits waiting for MCP stdio input, so it appears to do nothing until a client
connects. That is normal. Ctrl+C to exit.

Register it in your MCP client as a stdio command, same pattern as any other
server.

## Notes earned the hard way

- `mcp` is pinned below 2. mcp 2.0.0 renamed `mcp.server.fastmcp` to
  `mcp.server.mcpserver`, which breaks the import silently at server start.
  Stay on the 1.x that actually loads.
- Warmup time is not padding. Webcams open dark and auto-exposure needs a
  moment. Below about one second the frame comes back black or badly exposed.
  The default is 1.5 seconds for a reason.
- A failed capture is almost always a device already in use. Zoom, Teams, or a
  browser tab holding the camera produces an ffmpeg I/O error. The tools detect
  this and say so. The second most common cause is the OS privacy setting
  blocking desktop apps from the camera or microphone.
- Whisper model size: `base` (the default) is the speed and accuracy sweet spot.
  `small` is meaningfully better on strong accents at roughly three times the
  compute. Models cache under the user profile after the first download.
- Models load lazily and stay cached per size for the life of the server
  process. Reloading per call costs seconds and a lot of RAM.
- The default wake model is openWakeWord's pretrained `hey_jarvis`. No training,
  no dataset, it ships in the box. At the default 0.5 threshold it is reliable,
  and false positives are not a practical concern.

## Voice and speech limits

`speak` uses the legacy SAPI voices by default. They sound robotic. Modern OS
natural voices are much better and run locally once installed, and `speak`
picks them up automatically once they exist.

The honest architectural limit is that this is a four-hop pipeline: speech to
text, text to model, model to text, text to speech. It is slower than a native
audio model, and converting speech to text discards tone, volume, and emotion
before the model ever sees the words. A one-hop native audio setup would fix
both, but that requires a provider that accepts audio natively. Within a text
MCP harness, four hops is the correct design, not a shortcut.

`capture_stitched` only earns its keep when a second camera is actually
connected. Otherwise the second half of the image is a connection spinner.

## Output files

Captures default to a temp folder with timestamped names. Pass `output_path` to
send them somewhere durable. `record_and_transcribe` deletes its WAV afterwards
unless you pass `keep_audio=True`.

Nothing here prunes old captures automatically. Clear that folder occasionally.

## Selftest

```
uv run server.py --selftest
```

Prints the device list without needing an MCP client, which is the fastest way
to confirm the install is sane.

## License

MIT. See LICENSE.