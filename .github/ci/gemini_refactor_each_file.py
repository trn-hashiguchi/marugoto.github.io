#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

from pathspec import PathSpec  # gitwildmatch

# ====== 環境 ======
WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", ".")).resolve()
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "develop")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
THRESHOLD = int(os.getenv("LLM_RATING_THRESHOLD", "3"))
MAX_PRS = int(os.getenv("LLM_MAX_PRS", "10"))
DRY_RUN = os.getenv("LLM_DRY_RUN", "false").lower() == "true"

INCLUDE_GLOBS = [g.strip() for g in os.getenv("LLM_INCLUDE_GLOBS", "").split(",") if g.strip()]
EXCLUDE_GLOBS = [g.strip() for g in (os.getenv("LLM_EXCLUDE_GLOBS") or "").split(",") if g.strip()]

# 安全なデフォルト除外（呼び出し側 default と一致）
DEFAULT_EXCLUDES = [
    "build/**","dist/**","vendor/**","vender/**","node_modules/**",".git/**",".github/**",
    "**/*.min.js","**/*.min.css","**/*.svg","**/*.png","**/*.jpg","**/*.jpeg","**/*.gif",
    "**/*.pdf","**/*.zip","**/*.gz","**/*.tgz","**/*.jar","**/*.lock",
    "**/package-lock.json","**/pnpm-lock.yaml","**/yarn.lock","**/package.json",
    "**/.DS_Store","**/__pycache__/**","coverage/**"
]

# ====== ユーティリティ ======
def run(cmd: str, check: bool = True, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, check=check, cwd=cwd, env=env)

def git(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(f"git {cmd}", check=check)

def list_tracked_files() -> List[str]:
    res = git("ls-files")
    files = [ln for ln in res.stdout.splitlines() if ln.strip()]
    # include 指定があれば include -> exclude 順に適用
    inc_spec = PathSpec.from_lines("gitwildmatch", INCLUDE_GLOBS or ["**/*"])
    exc_spec = PathSpec.from_lines("gitwildmatch", (EXCLUDE_GLOBS or []) + DEFAULT_EXCLUDES)
    out = [f for f in files if inc_spec.match_file(f) and not exc_spec.match_file(f)]
    return out

def is_probably_text(path: Path) -> bool:
    try:
        b = path.read_bytes()[:8000]
        return b"\x00" not in b
    except Exception:
        return False

def sanitize_branch_component(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)[:60].strip("-")
    return s or str(int(time.time()))

def load_rules() -> str:
    p = WORKSPACE / "docs" / "rule.md"
    if not p.exists():
        return ""
    # 過大プロンプト回避（64KB）
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if len(txt.encode("utf-8")) > 64 * 1024:
        txt = txt[:64 * 1024]
    return txt

# ====== LLM 呼び出し ======
PROMPT_TEMPLATE = """あなたは経験豊富なシニアソフトウェアエンジニアです。
以下の**単一ファイル**をレビューし、必要な場合のみ安全なリファクタを提案してください。
**出力は必ずJSONのみ**（コードフェンス/説明文/前後テキスト禁止）。

入力:
- file_path: {path}
- coding_rules (存在しない場合は「なし」): <<RULES_START>>
{rules}
<<RULES_END>>
- file_content (UTF-8そのまま): <<FILE_START>>
{content}
<<FILE_END>>

判定基準（refactor_level 1〜5; 1=不要, 2=軽微, 3=中程度, 4=明確な技術的負債, 5=重大な欠陥/バグ/セキュリティ懸念）。
**無理に修正点を探さない**こと。スタイルのみ/好みの差は 1〜2 とし、needs_refactor=false とする。

JSONスキーマ:
{{
  "file": "相対パス",
  "refactor_level": 1|2|3|4|5,
  "reason": "日本語で簡潔に。根拠・影響範囲・リスク",
  "needs_refactor": true|false,
  "new_content_b64": "needs_refactor=true の時のみ。UTF-8の新ソースをBase64化。falseならnull"
}}

制約:
- 機能仕様を変えない安全な改善に限定。
- JSON以外の出力・diffやコードブロックは禁止。
"""

def call_gemini_cli(prompt: str) -> str | None:
    # Vertex AI 利用時は GEMINI_API_KEY が空であることを推奨（競合回避）
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)

    # 非対話モード: STDIN からプロンプトを渡す
    cmd = f"cat <<'EOF' | gemini -m {shlex.quote(MODEL)}\n{prompt}\nEOF"
    try:
        cp = run(cmd, check=True, env=env)
        out = cp.stdout.strip()
        # 場合により前後に装飾が付く可能性を考慮し、最初の '{' 〜 最後の '}' を抽出
        m = re.search(r"{.*}", out, flags=re.S)
        return m.group(0) if m else out
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"[gemini-cli] error: {e.stderr}\n")
        return None

def call_genai_sdk(prompt: str) -> str | None:
    # 構造化出力: application/json（SDK側はVertex/ADC設定を継承）
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(vertexai=True,
                              project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                              location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
        cfg = types.GenerateContentConfig(response_mime_type="application/json")
        resp = client.models.generate_content(model=MODEL, contents=prompt, config=cfg)
        return (resp.text or "").strip()
    except Exception as e:
        sys.stderr.write(f"[genai-sdk] fallback failed: {e}\n")
        return None

def review_one_file(path: Path, rules: str) -> dict | None:
    content = path.read_text(encoding="utf-8", errors="ignore")
    prompt = PROMPT_TEMPLATE.format(path=str(path), rules=(rules or "なし"), content=content)

    raw = call_gemini_cli(prompt)
    if not raw or not raw.strip():
        raw = call_genai_sdk(prompt)

    # JSONパース（緩和：先頭/末尾のノイズを除去）
    if not raw:
        return None
    try:
        # 最も外側のJSONオブジェクトに寄せる
        m = re.search(r"{.*}", raw, flags=re.S)
        data = json.loads(m.group(0) if m else raw)
        return data
    except Exception as e:
        sys.stderr.write(f"[parse] JSON parse failed for {path}: {e}\nraw={raw[:300]}...\n")
        return None

# ====== PR 作成 ======
def ensure_on_branch(branch: str):
    run("git fetch origin --prune", check=True)
    run(f"git checkout {shlex.quote(branch)}", check=True)
    run(f"git pull --ff-only origin {shlex.quote(branch)}", check=True)

def create_branch(base_branch: str, for_file: Path) -> str:
    name = f"refactor/{sanitize_branch_component(for_file.as_posix())}-{int(time.time())}"
    run(f"git checkout -b {shlex.quote(name)} {shlex.quote(base_branch)}", check=True)
    return name

def commit_and_push(file_path: Path, level: int, reason: str, branch: str):
    run(f"git add -- {shlex.quote(file_path.as_posix())}", check=True)
    title = f"refactor: {file_path.as_posix()} (level {level}/5)"
    body = f"自動リファクタ提案\n\n- refactor_level: **{level}/5**\n- reason: {reason}\n"
    run(f'git commit -m {shlex.quote(title)} -m {shlex.quote(body)}', check=True)
    run(f"git push -u origin {shlex.quote(branch)}", check=True)
    return title, body

def open_pr(base: str, head: str, title: str, body: str) -> Tuple[str, str]:
    env = os.environ.copy()
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN/GH_TOKEN is not set")
    # PR作成
    create_cmd = f"gh pr create -B {shlex.quote(base)} -H {shlex.quote(head)} --title {shlex.quote(title)} --body {shlex.quote(body)}"
    cp = run(create_cmd, check=True, env=env)
    url = (cp.stdout or "").strip().splitlines()[-1]
    # number 取得
    num = run(f"gh pr view {shlex.quote(head)} --json number -q .number", check=True, env=env).stdout.strip()
    return num, url

def comment_to_pr(selector: str, level: int, reason: str):
    text = f"### 🤖 リファクタレベル: **{level}/5**\n\n**理由**: {reason}\n"
    run(f"gh pr comment {shlex.quote(selector)} -b {shlex.quote(text)}", check=True)

# ====== メイン ======
def main() -> int:
    ensure_on_branch(TARGET_BRANCH)
    files = [Path(f) for f in list_tracked_files()]
    files = [f for f in files if f.exists() and is_probably_text(f)]

    rules = load_rules()
    created = 0

    for f in files:
        if created >= MAX_PRS:
            print(f"[info] PR 上限 {MAX_PRS} 件に達したため打ち切り。")
            break

        data = review_one_file(f, rules)
        if not data:
            print(f"[skip] LLM応答なし/JSON不正: {f}")
            continue

        needs = bool(data.get("needs_refactor"))
        level = int(data.get("refactor_level") or 0)
        reason = str(data.get("reason") or "").strip()

        if not needs or level < THRESHOLD:
            print(f"[skip] {f} needs_refactor={needs} level={level} < {THRESHOLD}")
            continue

        b64 = data.get("new_content_b64")
        if not b64:
            print(f"[skip] {f} new_content_b64 が空")
            continue

        new_src = base64.b64decode(b64).decode("utf-8", errors="ignore")

        # 変更がない場合はスキップ
        old_src = f.read_text(encoding="utf-8", errors="ignore")
        if new_src.strip() == old_src.strip():
            print(f"[skip] {f} 差分なし")
            continue

        if DRY_RUN:
            print(f"[dry-run] {f} level={level} reason={reason[:80]}...")
            continue

        # ブランチ作ってコミット & PR
        branch = create_branch(TARGET_BRANCH, f)
        f.write_text(new_src, encoding="utf-8")
        title, body = commit_and_push(f, level, reason, branch)
        pr_num, pr_url = open_pr(TARGET_BRANCH, branch, title, body)
        comment_to_pr(pr_num, level, reason)
        created += 1
        print(f"[created] PR #{pr_num} {pr_url} for {f}")

    if created == 0:
        print("[done] 作成PRなし（全ファイルが閾値未満/差分なし/不要判定でした）")

    return 0

if __name__ == "__main__":
    sys.exit(main())
