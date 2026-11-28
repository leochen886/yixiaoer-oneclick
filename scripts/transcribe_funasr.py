import json
import sys

def transcribe(audio_path, *, offline=False):
    from funasr import AutoModel

    models = {"model": "paraformer-zh", "vad_model": "fsmn-vad", "punc_model": "ct-punc"}
    if offline:
        from funasr.download.name_maps_from_hub import name_maps_ms
        from modelscope.hub.snapshot_download import snapshot_download

        for key, alias in models.items():
            model_id = name_maps_ms[alias]
            try:
                models[key] = snapshot_download(model_id, local_files_only=True)
            except Exception as exc:
                raise RuntimeError(f"无法读取本地语音模型 {model_id}，请检查 ModelScope 缓存。") from exc

    model = AutoModel(**models, device="cpu", disable_update=True, disable_pbar=offline)
    return model.generate(input=str(audio_path), batch_size_s=300, hotword="", return_raw_text=True)


if __name__ == "__main__":
    audio_path, out_path = sys.argv[1], sys.argv[2]
    result = transcribe(audio_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"done: {len(result)} result(s)")
