$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\..')
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$path = [IO.Path]::GetFullPath('research_results\raw\synthetic-voice.wav')
try {
    $voice.SetOutputToWaveFile($path, $format)
    $voice.Speak('Train number one two zero one is arriving at platform two.')
} finally { $voice.Dispose() }
& .\backend\.venv\Scripts\python.exe research_results\scripts\voice_smoke.py
if ($LASTEXITCODE -ne 0) { throw 'Voice smoke failed' }
