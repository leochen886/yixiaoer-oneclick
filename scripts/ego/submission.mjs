import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

export function submission(runDir, key, videoHash = key) {
  const keys = [key, videoHash];
  if (!keys.every(k => typeof k === "string" && /^[a-f\d]{64}$/i.test(k))) throw new Error("缺少发布素材校验值");
  const directory = path.join(path.dirname(runDir), ".submissions");
  const claims = [...new Set(keys.map(k => k.toLowerCase()))].sort().map(k => path.join(directory, k + ".json"));
  const receipt = path.join(runDir, "submit-result.txt");
  const acquired = [];

  async function status(claim) {
    let raw;
    try {
      raw = await readFile(claim, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw error;
    }
    let previous;
    try {
      previous = JSON.parse(raw).runDir;
      if ((await readFile(path.join(previous, "submit-result.txt"), "utf8")).trim() === "RESULT=OK") {
        return "RESULT=ALREADY_PUBLISHED " + previous;
      }
    } catch { /* A competing process may still be persisting its claim or receipt. */ }
    return "RESULT=UNCONFIRMED 请核对上次提交：" + (previous || claim);
  }

  async function existing() {
    for (const claim of claims) {
      const prior = await status(claim);
      if (prior) return prior;
    }
    return null;
  }

  async function release() {
    for (const claim of acquired) await unlink(claim);
    acquired.length = 0;
  }

  async function reserve() {
    await mkdir(directory, { recursive: true });
    for (const claim of claims) {
      let handle;
      try {
        handle = await open(claim, "wx");
        acquired.push(claim);
        await handle.writeFile(JSON.stringify({ runDir, keys, time: new Date().toISOString() }));
        await handle.sync();
      } catch (error) {
        if (handle) { await handle.close(); handle = null; }
        await release();
        if (error.code !== "EEXIST") throw error;
        return await status(claim) || "RESULT=NOT_CLICKED 另一次操作刚结束，请重试";
      } finally {
        if (handle) await handle.close();
      }
    }
    console.log("PUBLISH_SUBMITTING");
    return null;
  }

  async function finish(result) {
    if (!acquired.length) return;
    await writeFile(receipt + ".tmp", result, "utf8");
    await rename(receipt + ".tmp", receipt);
    if (result.startsWith("RESULT=NOT_CLICKED")) await release();
  }
  return { existing, reserve, finish, get attempted() { return acquired.length > 0; } };
}
