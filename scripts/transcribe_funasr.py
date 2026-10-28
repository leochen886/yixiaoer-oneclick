import json
import sys

from funasr import AutoModel

audio_path, out_path = sys.argv[1], sys.argv[2]

model = AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    device="cpu",
    disable_update=True,
)

result = model.generate(input=audio_path, batch_size_s=300, hotword="")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"done: {len(result)} result(s)")
