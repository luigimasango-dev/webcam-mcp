# Speaks text via the Windows SAPI engine.
#
# Everything comes in through environment variables and the text itself is read
# from a file — never interpolated into this script. Spoken text is arbitrary
# model output, and building a command string out of it would make any sentence
# containing quotes or $( ) executable.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$text = [IO.File]::ReadAllText($env:TTS_TEXT_FILE, [Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

if ($env:TTS_VOICE) {
    $match = $synth.GetInstalledVoices() |
        Where-Object { $_.VoiceInfo.Name -like "*$($env:TTS_VOICE)*" } |
        Select-Object -First 1
    if ($null -eq $match) {
        $names = ($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }) -join ', '
        throw "No voice matching '$($env:TTS_VOICE)'. Installed: $names"
    }
    $synth.SelectVoice($match.VoiceInfo.Name)
}

if ($env:TTS_RATE)   { $synth.Rate   = [int]$env:TTS_RATE }
if ($env:TTS_VOLUME) { $synth.Volume = [int]$env:TTS_VOLUME }

if ($env:TTS_WAV_OUT) {
    $synth.SetOutputToWaveFile($env:TTS_WAV_OUT)
} else {
    $synth.SetOutputToDefaultAudioDevice()
}

$synth.Speak($text)
$used = $synth.Voice.Name
$synth.Dispose()
Write-Output "spoken:$used"
