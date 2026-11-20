import json
import os
import sys
import wave

import numpy as np

def transcribe(audio_path, model_dir, model_source="small", *, offline=False):
    if not offline:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel

    with wave.open(str(audio_path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError("需要 16 kHz、16 位单声道 WAV。")
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio = samples.astype(np.float32) / 32768
    model = WhisperModel(model_source, device="cpu", compute_type="int8",
                         download_root=str(model_dir), local_files_only=offline)
    options = {"condition_on_previous_text": False, "initial_prompt": "以下是简体中文口播的逐字记录。"} if offline else {}
    segments, info = model.transcribe(audio, language="zh", beam_size=5, vad_filter=True, **options)
    return {"language": info.language, "duration": info.duration, "segments": [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments
    ]}


if __name__ == "__main__":
    audio_path, model_dir, out_path = sys.argv[1:4]
    result = transcribe(audio_path, model_dir, sys.argv[4] if len(sys.argv) > 4 else "small")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"done: {len(result['segments'])} segments")
