"""MCP server exposing local webcam capture and microphone transcription.

Everything here runs entirely on this machine: ffmpeg for capture, faster-whisper
for speech-to-text. No audio, video or transcript leaves the host.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("webcam-mcp")

_FFMPEG_FALLBACKS = [
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")),
]

_DEVICE_LINE = re.compile(r'^\[[^\]]+\]\s+"(?P<name>.+)"\s+\((?P<kind>video|audio)\)\s*$')

_DEFAULT_OUTPUT_DIR = Path(os.path.expandvars(r"%TEMP%\claude-webcam"))

# faster-whisper models are lazy-loaded and cached per size — reloading a model
# on every call costs several seconds and a chunk of a 16GB machine's RAM.
_MODEL_CACHE: dict[str, object] = {}

# Kept separate from the whisper cache so the two can never collide on a key.
_WAKE_CACHE: dict[str, object] = {}


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in _FFMPEG_FALLBACKS:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "ffmpeg not found on PATH. Install it with `winget install Gyan.FFmpeg` "
        "and restart the session."
    )


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg did not finish within {timeout}s. The device may be in use by "
            "another application (Zoom, Teams, Chrome) — close it and retry."
        )


def _resolve_output(output_path: Optional[str], prefix: str, suffix: str) -> Path:
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    _DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_OUTPUT_DIR / f"{prefix}-{int(time.time())}{suffix}"


def _list_dshow_devices() -> dict[str, list[str]]:
    """Enumerate DirectShow capture devices. ffmpeg prints these to stderr and
    exits non-zero by design, so the return code is deliberately ignored."""
    proc = _run(
        [_ffmpeg(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        timeout=30,
    )
    devices: dict[str, list[str]] = {"video": [], "audio": []}
    for line in proc.stderr.splitlines():
        match = _DEVICE_LINE.match(line.strip())
        if match:
            devices[match.group("kind")].append(match.group("name"))
    if not devices["video"] and not devices["audio"]:
        raise RuntimeError(
            "No DirectShow devices found. Check Windows camera/microphone privacy "
            "settings (Settings > Privacy & security > Camera / Microphone) and that "
            "desktop apps are allowed access.\n\nffmpeg said:\n" + proc.stderr[-1500:]
        )
    return devices


def _default_device(kind: str, requested: Optional[str]) -> str:
    devices = _list_dshow_devices()
    available = devices[kind]
    if not available:
        raise RuntimeError(f"No {kind} capture devices are available on this machine.")
    if requested is None:
        return available[0]
    for name in available:
        if name.lower() == requested.lower():
            return name
    for name in available:
        if requested.lower() in name.lower():
            return name
    raise RuntimeError(
        f"No {kind} device matching {requested!r}. Available: {', '.join(available)}"
    )


def _capture_hint(stderr: str) -> str:
    lowered = stderr.lower()
    if "i/o error" in lowered or "could not run filter" in lowered:
        return " The device is most likely already open in another app (Zoom, Teams, Chrome)."
    if "permission" in lowered or "access is denied" in lowered:
        return " Windows privacy settings may be blocking desktop apps from the device."
    return ""


@mcp.tool()
def list_devices() -> str:
    """List the webcams and microphones available for capture on this machine.

    Call this first if a capture fails, or to find the exact device name to pass
    to the other tools."""
    devices = _list_dshow_devices()
    lines = ["Video devices (cameras):"]
    lines += [f"  [{i}] {n}" for i, n in enumerate(devices["video"])] or ["  (none)"]
    lines.append("")
    lines.append("Audio devices (microphones):")
    lines += [f"  [{i}] {n}" for i, n in enumerate(devices["audio"])] or ["  (none)"]
    lines.append("")
    lines.append("The first device of each kind is the default when none is specified.")
    return "\n".join(lines)


@mcp.tool()
def capture_frame(
    device: Optional[str] = None,
    warmup_seconds: float = 1.5,
    output_path: Optional[str] = None,
) -> str:
    """Capture a single still frame from a webcam and save it as a JPEG.

    Returns the path to the image, which can then be opened with the Read tool to
    actually view it.

    Args:
        device: Camera name (substring match is fine). Defaults to the first camera.
        warmup_seconds: Stream time discarded before grabbing the frame. Webcams
            open dark and auto-exposure needs a moment; below ~1s frames come out
            black or badly exposed.
        output_path: Where to write the JPEG. Defaults to a timestamped file in
            %TEMP%\\claude-webcam.
    """
    if warmup_seconds < 0:
        raise ValueError("warmup_seconds cannot be negative.")
    camera = _default_device("video", device)
    destination = _resolve_output(output_path, "frame", ".jpg")

    proc = _run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", f"video={camera}",
            "-ss", str(warmup_seconds), "-frames:v", "1", "-q:v", "2",
            str(destination),
        ],
        timeout=int(warmup_seconds) + 60,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(
            f"Capture from {camera!r} produced no image.{_capture_hint(proc.stderr)}"
            f"\n\nffmpeg said:\n{proc.stderr[-1500:]}"
        )
    size_kb = destination.stat().st_size / 1024
    return (
        f"Captured a frame from {camera!r} ({size_kb:.0f} KB).\n"
        f"Saved to: {destination}\n\n"
        f"Open that path with the Read tool to view the image."
    )


@mcp.tool()
def capture_stitched(
    top_device: Optional[str] = None,
    bottom_device: Optional[str] = None,
    warmup_seconds: float = 1.5,
    width: int = 1280,
    output_path: Optional[str] = None,
) -> str:
    """Capture from two cameras at once and stack them into a single image.

    Top half is the first camera, bottom half the second. One image means one
    Read call and one coherent view, instead of two frames that have to be
    mentally reassembled.

    Args:
        top_device: Camera for the top half. Defaults to the first camera.
        bottom_device: Camera for the bottom half. Defaults to the second camera.
        warmup_seconds: Stream time discarded per camera before grabbing a frame.
        width: Both frames are scaled to this width before stacking — vstack
            requires matching widths and the two cameras rarely agree.
        output_path: Where to write the stitched JPEG.
    """
    cameras = _list_dshow_devices()["video"]
    if len(cameras) < 2 and (top_device is None or bottom_device is None):
        raise RuntimeError(
            f"Stitching needs two cameras; this machine reports {len(cameras)}: "
            f"{', '.join(cameras) or 'none'}. If a phone-as-webcam is expected, its "
            "app must be running for the virtual camera to appear."
        )
    top = _default_device("video", top_device if top_device else cameras[0])
    bottom = _default_device("video", bottom_device if bottom_device else cameras[1])
    if top == bottom:
        raise RuntimeError(
            f"Top and bottom resolved to the same camera ({top!r}). Most webcams "
            "cannot be opened twice at once — pass two different devices."
        )

    destination = _resolve_output(output_path, "stitched", ".jpg")
    temp_frames = []
    try:
        # Captured sequentially, not concurrently: two dshow devices opened at the
        # same instant contend for USB bandwidth and one usually fails to start.
        for label, camera in (("top", top), ("bottom", bottom)):
            frame = _resolve_output(None, f"stitch-{label}", ".jpg")
            temp_frames.append(frame)
            proc = _run(
                [
                    _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "dshow", "-i", f"video={camera}",
                    "-ss", str(warmup_seconds), "-frames:v", "1", "-q:v", "2",
                    str(frame),
                ],
                timeout=int(warmup_seconds) + 60,
            )
            if not frame.exists() or frame.stat().st_size == 0:
                raise RuntimeError(
                    f"The {label} camera {camera!r} produced no image."
                    f"{_capture_hint(proc.stderr)}\n\nffmpeg said:\n{proc.stderr[-1000:]}"
                )

        proc = _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(temp_frames[0]), "-i", str(temp_frames[1]),
                "-filter_complex",
                f"[0:v]scale={width}:-2[t];[1:v]scale={width}:-2[b];[t][b]vstack=inputs=2",
                "-q:v", "2", str(destination),
            ],
            timeout=60,
        )
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError(f"Stitching failed.\n\nffmpeg said:\n{proc.stderr[-1000:]}")
    finally:
        for frame in temp_frames:
            frame.unlink(missing_ok=True)

    size_kb = destination.stat().st_size / 1024
    return (
        f"Stitched two cameras into one image ({size_kb:.0f} KB).\n"
        f"  Top half:    {top}\n"
        f"  Bottom half: {bottom}\n"
        f"Saved to: {destination}\n\n"
        f"Open that path with the Read tool to view it."
    )


@mcp.tool()
def list_voices() -> str:
    """List the text-to-speech voices installed on this machine."""
    proc = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName System.Speech; "
         "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() "
         "| ForEach-Object { $_.VoiceInfo.Name + '  [' + $_.VoiceInfo.Culture + ', ' "
         "+ $_.VoiceInfo.Gender + ']' }"],
        timeout=60,
    )
    voices = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not voices:
        raise RuntimeError(f"Could not enumerate voices.\n{proc.stderr[-1000:]}")
    return "Installed voices:\n" + "\n".join(f"  {v}" for v in voices) + (
        "\n\nThese are the legacy SAPI voices. Windows 11's far better natural "
        "voices install via Settings > Accessibility > Narrator > Add natural voices."
    )


@mcp.tool()
def speak(
    text: str,
    voice: Optional[str] = None,
    rate: int = 0,
    volume: int = 100,
    save_to: Optional[str] = None,
) -> str:
    """Say something out loud through the speakers.

    Args:
        text: What to say.
        voice: Voice name or fragment, e.g. 'Zira'. Defaults to the system voice.
        rate: Speed from -10 (slowest) to 10 (fastest). 0 is normal.
        volume: 0-100.
        save_to: Write the speech to a WAV file instead of playing it aloud.
    """
    if not text.strip():
        raise ValueError("Nothing to say — text is empty.")
    if not -10 <= rate <= 10:
        raise ValueError("rate must be between -10 and 10.")
    if not 0 <= volume <= 100:
        raise ValueError("volume must be between 0 and 100.")

    script = Path(__file__).with_name("tts.ps1")
    if not script.exists():
        raise RuntimeError(f"Missing TTS helper script at {script}")

    # The text goes via a file, never onto a command line — see the note in tts.ps1.
    text_file = _resolve_output(None, "tts-text", ".txt")
    text_file.write_text(text, encoding="utf-8")

    env = dict(os.environ)
    env["TTS_TEXT_FILE"] = str(text_file)
    env["TTS_RATE"] = str(rate)
    env["TTS_VOLUME"] = str(volume)
    if voice:
        env["TTS_VOICE"] = voice
    else:
        env.pop("TTS_VOICE", None)
    if save_to:
        destination = Path(save_to).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        env["TTS_WAV_OUT"] = str(destination)
    else:
        env.pop("TTS_WAV_OUT", None)

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, env=env,
            timeout=max(60, len(text) // 5),
            check=False,
        )
    finally:
        text_file.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(f"Speech failed.\n{(proc.stderr or proc.stdout)[-1500:]}")

    used = next(
        (l.split(":", 1)[1] for l in proc.stdout.splitlines() if l.startswith("spoken:")),
        "system default",
    )
    if save_to:
        return f"Wrote {len(text)} characters of speech to {save_to} (voice: {used})."
    return f'Said aloud in {used}: "{text}"'


@mcp.tool()
def listen_for_wake_word(
    timeout_seconds: float = 60.0,
    record_seconds: float = 8.0,
    threshold: float = 0.5,
    device: Optional[str] = None,
    model_size: str = "base",
) -> str:
    """Wait for "hey Jarvis" to be spoken, then record what follows and transcribe it.

    Blocks while listening. Detection runs locally via openWakeWord — audio is
    processed in memory and only the phrase after the wake word is written to disk,
    then deleted.

    Args:
        timeout_seconds: Give up if the wake word is not heard within this long.
        record_seconds: How long to record after detection.
        threshold: Detection confidence 0-1. Raise it if it triggers on unrelated
            speech, lower it if it misses genuine attempts.
        device: Microphone name. Defaults to the first mic.
        model_size: Whisper model for transcribing the phrase that follows.
    """
    if not 0 < timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be between 0 and 600.")
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1.")

    try:
        import numpy as np
        from openwakeword.model import Model
    except ImportError:
        raise RuntimeError(
            "openwakeword is not installed in this server's environment. "
            "Run: uv pip install openwakeword onnxruntime"
        )

    mic = _default_device("audio", device)
    if "hey_jarvis" not in _WAKE_CACHE:
        _WAKE_CACHE["hey_jarvis"] = Model(
            wakeword_models=["hey_jarvis"], inference_framework="onnx"
        )
    detector = _WAKE_CACHE["hey_jarvis"]
    detector.reset()

    # Raw 16kHz mono PCM straight out of ffmpeg, rather than adding a second audio
    # backend (PortAudio/PyAudio) purely for streaming. openWakeWord wants 1280
    # samples per call, which is 2560 bytes of int16.
    frame_bytes = 1280 * 2
    stream = subprocess.Popen(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "dshow", "-i", f"audio={mic}",
            "-ar", "16000", "-ac", "1", "-f", "s16le", "-",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + timeout_seconds
    best = 0.0
    detected = False
    try:
        while time.monotonic() < deadline:
            chunk = stream.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                err = stream.stderr.read().decode(errors="replace")
                raise RuntimeError(
                    f"Microphone stream from {mic!r} ended unexpectedly."
                    f"{_capture_hint(err)}\n\nffmpeg said:\n{err[-1000:]}"
                )
            scores = detector.predict(np.frombuffer(chunk, dtype=np.int16))
            score = max(scores.values())
            best = max(best, score)
            if score >= threshold:
                detected = True
                break
    finally:
        stream.terminate()
        try:
            stream.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stream.kill()

    if not detected:
        return (
            f"Did not hear 'hey Jarvis' within {timeout_seconds:g}s. "
            f"Highest confidence reached was {best:.2f} against a {threshold} threshold"
            + (
                " — close, so try speaking a little clearer or lower the threshold."
                if best > threshold * 0.6
                else "."
            )
        )

    spoken = record_and_transcribe(
        seconds=record_seconds, device=mic, model_size=model_size
    )
    return f"Heard 'hey Jarvis' (confidence {best:.2f}). Listening...\n\n{spoken}"


@mcp.tool()
def record_audio(
    seconds: float = 10.0,
    device: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Record from a microphone to a 16kHz mono WAV file.

    This only records — it does not transcribe. Use record_and_transcribe to get
    text back, or pass the returned path to transcribe_file.

    Args:
        seconds: Recording length. Blocks for roughly this long.
        device: Microphone name (substring match is fine). Defaults to the first mic.
        output_path: Where to write the WAV. Defaults to %TEMP%\\claude-webcam.
    """
    if not 0 < seconds <= 300:
        raise ValueError("seconds must be between 0 and 300.")
    mic = _default_device("audio", device)
    destination = _resolve_output(output_path, "audio", ".wav")

    proc = _run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", f"audio={mic}",
            "-t", str(seconds), "-ar", "16000", "-ac", "1",
            str(destination),
        ],
        timeout=int(seconds) + 60,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(
            f"Recording from {mic!r} produced no audio.{_capture_hint(proc.stderr)}"
            f"\n\nffmpeg said:\n{proc.stderr[-1500:]}"
        )
    size_kb = destination.stat().st_size / 1024
    return f"Recorded {seconds:g}s from {mic!r} ({size_kb:.0f} KB).\nSaved to: {destination}"


def _load_model(model_size: str):
    if model_size not in _MODEL_CACHE:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "faster-whisper is not installed in this server's environment. "
                "Run: uv pip install faster-whisper"
            )
        # int8 on CPU: this is a 16GB laptop with no CUDA assumption. 'base' is
        # the accuracy/speed sweet spot; 'small' is noticeably better on accents
        # at roughly 3x the compute.
        _MODEL_CACHE[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _MODEL_CACHE[model_size]


def _transcribe(path: Path, model_size: str, language: Optional[str]) -> str:
    model = _load_model(model_size)
    segments, info = model.transcribe(str(path), language=language, vad_filter=True)
    lines = [seg.text.strip() for seg in segments if seg.text.strip()]
    if not lines:
        return (
            f"(No speech detected in {path.name}. Detected language "
            f"{info.language!r} at {info.language_probability:.0%} confidence — "
            "the recording may be silent, or the mic captured nothing.)"
        )
    header = (
        f"Transcript of {path.name} "
        f"[{info.language} @ {info.language_probability:.0%}, {info.duration:.1f}s audio]:\n\n"
    )
    return header + " ".join(lines)


@mcp.tool()
def transcribe_file(
    file_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
) -> str:
    """Transcribe an existing audio or video file to text, locally.

    Args:
        file_path: Path to any audio/video file ffmpeg can decode.
        model_size: Whisper model — tiny, base, small, medium, large-v3. Larger is
            more accurate and slower. First use of a size downloads it (~150MB for
            base) and caches it under the user profile.
        language: ISO code such as 'en' to skip auto-detection. Auto-detect is
            usually fine but can misfire on very short clips.
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        raise RuntimeError(f"No such file: {path}")
    return _transcribe(path, model_size, language)


@mcp.tool()
def record_and_transcribe(
    seconds: float = 10.0,
    device: Optional[str] = None,
    model_size: str = "base",
    language: Optional[str] = None,
    keep_audio: bool = False,
) -> str:
    """Record from the microphone and return what was said, as text.

    Blocks for `seconds` while recording, then transcribes locally. Nothing is
    uploaded anywhere.

    Args:
        seconds: How long to record. Speak for roughly this long.
        device: Microphone name. Defaults to the first mic.
        model_size: Whisper model — tiny, base, small, medium, large-v3.
        language: ISO code such as 'en' to skip auto-detection.
        keep_audio: Keep the WAV file afterwards instead of deleting it.
    """
    if not 0 < seconds <= 300:
        raise ValueError("seconds must be between 0 and 300.")
    mic = _default_device("audio", device)
    destination = _resolve_output(None, "speech", ".wav")

    proc = _run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", f"audio={mic}",
            "-t", str(seconds), "-ar", "16000", "-ac", "1",
            str(destination),
        ],
        timeout=int(seconds) + 60,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(
            f"Recording from {mic!r} produced no audio.{_capture_hint(proc.stderr)}"
            f"\n\nffmpeg said:\n{proc.stderr[-1500:]}"
        )
    try:
        result = _transcribe(destination, model_size, language)
    finally:
        if not keep_audio:
            destination.unlink(missing_ok=True)
    footer = f"\n\n(Audio kept at {destination})" if keep_audio else "\n\n(Audio file deleted.)"
    return f"Recorded {seconds:g}s from {mic!r}.\n\n{result}{footer}"


if __name__ == "__main__":
    # `python server.py --selftest` exercises the tools without an MCP client.
    if "--selftest" in sys.argv:
        print(list_devices())
        sys.exit(0)
    mcp.run()
