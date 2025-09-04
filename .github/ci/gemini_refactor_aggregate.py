#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, base64, subprocess, shlex, time, logging
from pathlib import Path
from typing import Optional, Tuple
import pathspec

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "develop")
THRESHOLD = int(os.getenv("LLM_RATING_THRESHOLD", "3"))
INCLUDE = [p.strip() for p in os.getenv("LLM_INCLUDE_GLOBS","").split(",") if p.strip()]
EXCLUDE = [p.strip() for p in os.getenv("LLM_EXCLUDE_GLOBS","").split(",") if p.strip()]
DRY_RUN = os.getenv("LLM_DRY_RUN","false").lower() == "true"

BIN_EXT = {".png",".jpg",".jpeg",".gif",".pdf",".zip",".gz",".tgz",".jar",".so",".dylib",".exe",".dll"}
DEFAULT_EXCLUDE = {".git",".github","node_modules","build","dist","vendor","vender","coverage","__pycache__"}

def run(cmd: str, check=True, cwd=None, env=None) -> subprocess.CompletedProcess:
    logging.debug("+ %s", cmd)
    cp = subprocess.run(cmd, shell=True, text=True, capture_output=True, cwd=cwd, env=env)
    if check and cp.returncode != 0:
        logging.error(cp.stdout)
        logging.error(cp.stderr)
        raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)
    return cp

def ensure_base_and_branch(base: str) -> str:
    run("git fetch --prune origin", check=False)
    run(f"git checkout -B {shlex.quote(base)} origin/{shlex.quote(base)}", check=False)
    run(f"git pull --ff-only origin {shlex.quote(base)}", check=False)
    work = f"refactor/aggregate-{int(time.time())}"
    run(f"git checkout -b {shlex.quote(work)}", check=True)
    return work

def list_tracked_files() -> list[str]:
    out = run("git ls-files", check=True).stdout.splitlines()
    files = []
    for p in out:
        if not p or p.startswith(".git/"):
            continue
        path = Path(p)
        if path.suffix.lower() in BIN_EXT:
            continue
        if any(seg in DEFAULT_EXCLUDE for seg in path.parts):
            continue
        files.append(p)
    return files

def build_specs():
    inc = pathspec.PathSpec.from_lines("gitwildmatch", INCLUDE) if INCLUDE else None
    exc = pathspec.PathSpec.from_lines("gitwildmatch", EXCLUDE) if EXCLUDE else None
    return inc, exc

def filtered(files: list[str]) -> list[str]:
    inc, exc = build_specs()
    def ok(p: str) -> bool:
        if exc and exc.match_file(p):
            return False
        return True if not inc else bool(inc.match_file(p))
    return [p for p in files if ok(p)]

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")

def load_rules() -> str:
    f = Path("docs/rule.md")
    if f.exists():
        txt = read_text(f)
        return txt[:20000]
    return ""

def build_prompt(path: str, content: str, rules: str) -> str:
    rule_part = f"### Coding Rules (from docs/rule.md)\n{rules}\n" if rules else "### Coding Rules\n(特に規約はありません)\n"
    return f"""あなたは上級ソフトウェアエンジニアとして、以下の単一ファイルに対して**必要な場合のみ**保守性の高いリファクタを提案・適用します。無理に変更点を作らず、意味の薄い命名変更や単なるフォーマット調整のみの変更は避けてください。

{rule_part}

### 出力仕様（厳守）
**純粋なJSONのみ**を出力し、マークダウンや注釈は一切含めないこと:
{{
"file": "{path}",
"needs_refactor": true | false,
"refactor_level": 1 | 2 | 3 | 4 | 5,
"reason": "なぜ必要か。不要ならその理由",
"new_content_b64": "変更後ファイル全体をUTF-8でBase64。不要なら空文字"
}}

### 判定基準
- セキュリティ/バグ/可読性/テスト容易性/パフォーマンス/規約違反などが改善される場合のみ "needs_refactor": true
- 破壊的変更やAPI互換性を崩す変更は避ける
- レベル定義: 1=不要, 2=軽微, 3=推奨, 4=重要, 5=緊急

### 対象ファイル
- パス: {path}
- 現在の内容:
``` 
{content}
```
"""

def call_gemini_cli(prompt: str) -> Optional[str]:
    cmd = f"cat <<'EOF' | gemini -m {shlex.quote(MODEL)}\n{prompt}\nEOF"
    cp = run(cmd, check=False, env=os.environ.copy())
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()

def extract_json(text: str) -> Optional[dict]:
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s >= 0 and e > s:
            cand = text[s:e+1]
            return json.loads(cand)
    except Exception:
        return None
    return None

def call_gemini_sdk(prompt: str) -> Optional[dict]:
    try:
        from google import genai
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION","global")
        client = genai.Client(vertexai=True, project=project, location=location)
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={"response_mime_type":"application/json"}
        )
        txt = getattr(resp, "text", "") or ""
        return extract_json(txt) or json.loads(txt)
    except Exception as e:
        logging.error("SDK fallback failed: %s", e)
        return None

def review_and_maybe_rewrite(path: Path, rules: str):
    content = read_text(path)
    prompt = build_prompt(path.as_posix(), content, rules)
    out = call_gemini_cli(prompt)
    data = extract_json(out) if out else None
    if data is None:
        data = call_gemini_sdk(prompt)
    if not data:
        logging.warning("[skip] %s LLM応答なし", path)
        return None
    needs = bool(data.get("needs_refactor"))
    level = int(data.get("refactor_level") or 1)
    reason = str(data.get("reason") or "").strip()
    b64 = data.get("new_content_b64") or ""
    if not needs or level < THRESHOLD or not b64:
        logging.info("[skip] %s needs_refactor=%s level=%s < %s", path, needs, level, THRESHOLD)
        return None
    try:
        new_src = base64.b64decode(b64).decode("utf-8")
    except Exception:
        logging.warning("[skip] %s Base64デコード失敗", path)
        return None
    if new_src == content:
        logging.info("[skip] %s 変更なし", path)
        return None
    if not DRY_RUN:
        path.write_text(new_src, encoding="utf-8")
        title = f"refactor: {path.as_posix()} (level {level}/5)"
        body = f"自動リファクタ提案\n- refactor_level: **{level}/5**\n- reason: {reason}\n"
        run(f"git add -- {shlex.quote(path.as_posix())}")
        run(f"git commit -m {shlex.quote(title)} -m {shlex.quote(body)}")
    return (level, reason, path.as_posix())

def open_pr(base: str, head: str, summary: list[Tuple[str,int,str]]):
    title = f"refactor: {len(summary)} file(s) (threshold {THRESHOLD}/5)"
    body_lines = ["## 自動リファクタ結果"]
    for pth, lv, rsn in summary:
        body_lines.append(f"- `{pth}` → **{lv}/5**: {rsn}")
    body = "\n".join(body_lines)
    env = os.environ.copy()
    env["GH_TOKEN"] = env.get("GITHUB_TOKEN", env.get("GH_TOKEN",""))
    run(f"git push -u origin {shlex.quote(head)}", env=env)
    cp = run(f"gh pr create -B {shlex.quote(base)} -H {shlex.quote(head)} --title {shlex.quote(title)} --body {shlex.quote(body)}", env=env, check=False)
    if cp.returncode != 0:
        logging.error("gh pr create failed: %s %s", cp.stdout, cp.stderr)
        raise SystemExit(1)
    url = cp.stdout.strip()
    return (title, url)

def main() -> int:
    work_branch = ensure_base_and_branch(TARGET_BRANCH)
    files = filtered(list_tracked_files())
    if not files:
        logging.info("対象ファイルがありません。終了します。")
        return 0
    rules = load_rules()
    created = []
    for p in files:
        path = Path(p)
        if path.suffix.lower() in BIN_EXT:
            continue
        res = review_and_maybe_rewrite(path, rules)
        if res:
            created.append(res)
    if not created:
        logging.info("リファクタは不要でした（しきい値: %s/5）。PRは作成しません。", THRESHOLD)
        return 0
    if DRY_RUN:
        print("### Dry run summary")
        for fp, lv, rsn in created:
            print(f"- {fp} -> {lv}/5: {rsn}")
        return 0
    title, url = open_pr(TARGET_BRANCH, work_branch, created)
    print(f"[created] {title}\n{url}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
