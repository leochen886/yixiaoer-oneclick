import json
import os
import sys
import wave

import numpy as np

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from faster_whisper import WhisperModel

audio_path, model_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
model_source = sys.argv[4] if len(sys.argv) > 4 else "small"

with wave.open(audio_path, "rb") as w:
    assert w.getnchannels() == 1, "expect mono wav"
    assert w.getsampwidth() == 2, "expect 16-bit wav"
    assert w.getframerate() == 16000, "expect 16kHz wav"
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

model = WhisperModel(model_source, device="cpu", compute_type="int8", download_root=model_dir)
segments, info = model.transcribe(audio, language="zh", beam_size=5, vad_filter=True)
result = {
    "language": info.language,
    "duration": info.duration,
    "segments": [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments
    ],
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"done: {len(result['segments'])} segments")
