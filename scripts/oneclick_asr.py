"""One-click ASR helper: 16k mono wav -> plain UTF-8 text.

Uses FunASR's SeACo-Paraformer, which is already cached in the local
ModelScope cache, so it runs offline.
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from funasr import AutoModel

audio_path, out_path = sys.argv[1], sys.argv[2]

model = AutoModel(
    model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    device="cpu",
    disable_update=True,
)

result = model.generate(input=audio_path, batch_size_s=300)
text = "".join(item.get("text", "") for item in result).strip()

with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)

print(text)
