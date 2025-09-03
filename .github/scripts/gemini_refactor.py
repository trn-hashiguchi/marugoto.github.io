#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_BRANCH = os.environ.get("BASE_BRANCH", "develop")
NEW_BRANCH = f"auto/refactor-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
REPO_ROOT = Path(__file__).resolve().parents[2]

EXCLUDES = {"node_modules", "vendor", ".github", ".git", "dist", "build"}

PROMPT = r'''
あなたは厳格なコードレビュアです。以下を厳守して**JSONのみ**で出力してください。

- 目的：リポジトリ全体を俯瞰し、**本当に必要な**最小限のリファクタのみ提案する。
- 「無理して修正点を探さない」。可読性/安全性/パフォーマンス/保守性に**明確な根拠**がある場合のみ。
- 変更が不要なら "decision": "NO_CHANGES" とする。
- 変更が必要なら以下を返す：
  - "level": 1〜5（5=強い必要性）。**3未満ならPRを作らない**想定で評価。
  - "rationale": 「なぜこの修正を行うのか」を短く明確に（PR本文にそのまま使用）。
  - "title": わかりやすいPRタイトル。
  - "diff": HEADを基準としたunified diff（`git apply`可能、--unified=3以上の文脈を含む、余計な文字出力禁止）。
  - "notes": 箇条書き補足（任意）。
スキーマ：
{"decision":"","level":0,"rationale":"","title":"","diff":"","notes":[]}
'''

def write_env(name: str, value: str):
    """Append a key=value to $GITHUB_ENV (multi-line safe)."""
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        return
    if "\n" in value:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"{name}<<__EOF__\n{value}\n__EOF__\n")
    else:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")

def run(cmd: list[str], check=True, capture_output=False, text=True, cwd=None):
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=text, cwd=cwd)

def list_included_paths() -> list[str]:
    """Return repo paths excluding EXCLUDES; used to give Gemini some context if needed."""
    paths = []
    for p in REPO_ROOT.rglob("*"):
        if p.is_dir():
            # skip excluded dirs early
            parts = set(p.parts)
            if parts & EXCLUDES:
                continue
        else:
            if any(part in EXCLUDES for part in p.parts):
                continue
            # ignore large/binary-ish files heuristically
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".mp4", ".zip"}:
                continue
            if p.stat().st_size > 512_000:  # 512KB以上はスキップ
                continue
            rel = str(p.relative_to(REPO_ROOT))
            paths.append(rel)
    return paths[:2000]  # 上限で切る（CLIの入力サイズ対策）

def call_gemini() -> dict:
    """
    非対話で Gemini CLI を呼び出し、JSONのみを受け取る。
    CLIの基本用法（非対話/スクリプト実行）は公開情報を参照。 :contentReference[oaicite:2]{index=2}
    """
    # ここではプロンプトのみ渡す簡易実装。
    # 必要なら list_included_paths() でファイル名一覧を補助情報として渡すことも可能。
    proc = run(["gemini", "--json", "--prompt", PROMPT], capture_output=True)
    out = proc.stdout.strip()
    # JSONだけを想定。前後にノイズが混じる場合は抽出する。
    m = re.search(r'\{.*\}\s*$', out, re.S)
    if not m:
        raise RuntimeError("Gemini出力がJSONとして解釈できません")
    return json.loads(m.group(0))

def ensure_on_base_and_branch(new_branch: str):
    run(["git", "checkout", BASE_BRANCH])
    run(["git", "pull", "--ff-only"])
    run(["git", "checkout", "-b", new_branch])

def validate_unified_diff(diff_text: str) -> bool:
    return bool(re.match(r'^(diff --git|--- |\+\+\+ )', diff_text))

def apply_patch(diff_text: str):
    """
    `git apply` で安全に適用。unified diff（文脈行あり）を期待します。
    git-apply の安全性やオプション詳細は公式に記載があります。 :contentReference[oaicite:3]{index=3}
    """
    p = subprocess.Popen(["git", "apply", "--index", "--reject", "--whitespace=fix", "-"],
                         stdin=subprocess.PIPE, text=True)
    assert p.stdin is not None
    p.stdin.write(diff_text)
    p.stdin.close()
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("git apply に失敗しました")

def staged_changes_exist() -> bool:
    rc = run(["bash", "-lc", "git diff --cached --quiet || echo CHANGES"], capture_output=True).stdout.strip()
    return rc == "CHANGES"

def run_tests() -> bool:
    # 代表的なパターンのみ簡易に対応。必要に応じて拡張してください。
    try:
        if Path("package.json").exists():
            # npm test が定義されているかはここでは深く確認せず一旦実行
            run(["npm", "test"])
            return True
    except subprocess.CalledProcessError:
        return False

    try:
        if Path("composer.json").exists():
            if Path("vendor/bin/phpunit").exists():
                run(["vendor/bin/phpunit"])
                return True
    except subprocess.CalledProcessError:
        return False

    try:
        # Python: pytest があれば走る（上のジョブでインストール試行済み）
        if Path("pytest.ini").exists() or Path("tests").exists():
            run(["python", "-m", "pytest"])
            return True
    except subprocess.CalledProcessError:
        return False

    return True  # 何もなければ成功扱い

def main():
    try:
        result = call_gemini()
    except Exception as e:
        # 解析に失敗したらPRは作らない
        write_env("NEED_PR", "0")
        print(f"[gemini] error: {e}", file=sys.stderr)
        sys.exit(0)

    decision = result.get("decision", "")
    if decision == "NO_CHANGES":
        write_env("NEED_PR", "0")
        return

    level = int(result.get("level", 0) or 0)
    if level < 3:
        write_env("NEED_PR", "0")
        return

    diff_text = result.get("diff", "") or ""
    title = result.get("title", "Refactor: improvements")
    rationale = result.get("rationale", "Reason not provided").strip()

    if not diff_text or not validate_unified_diff(diff_text):
        write_env("NEED_PR", "0")
        return

    # ベースブランチから作業ブランチを切る
    ensure_on_base_and_branch(NEW_BRANCH)

    # パッチ適用
    try:
        apply_patch(diff_text)
    except Exception as e:
        # パッチ不正ならPRしない
        write_env("NEED_PR", "0")
        print(f"[git apply] error: {e}", file=sys.stderr)
        return

    if not staged_changes_exist():
        write_env("NEED_PR", "0")
        return

    # コミット
    run(["git", "commit", "-m", f"chore(refactor): apply Gemini suggestions (level={level})"])

    # テスト（失敗したらロールバック）
    if not run_tests():
        run(["git", "reset", "--hard", "HEAD~1"])
        run(["git", "checkout", BASE_BRANCH])
        write_env("NEED_PR", "0")
        return

    # PR本文（「なぜこの修正を行ったのか」＋レベルを必ず記載）
    pr_body = f"""### 目的（なぜこの修正を行ったのか）
{rationale}

### リファクタレベル
**{level} / 5**

> 本PRは Gemini 2.5 Pro による自動レビュー結果に基づく提案です。  
> 「無理して修正点を探さない」方針で、必要性が低い場合は PR を作成していません。
"""

    # 後続ステップ用の環境変数
    write_env("NEED_PR", "1")
    write_env("NEW_BRANCH", NEW_BRANCH)
    write_env("PR_TITLE", title)
    write_env("PR_BODY", pr_body)

if __name__ == "__main__":
    main()
