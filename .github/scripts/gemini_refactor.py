#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto-refactor (Vertex AI / ADC) for GitHub Actions.
- ベースブランチ（入力 or 自動特定）からの差分や全文を LLM に渡し、必要最小限のリファクタのみを提案。
- LLM から構造化JSON（decision/level/rationale/title/diff/notes）を受け取り、
  diff を git apply -> commit -> 後段ジョブで PR 作成。
- 変更不要 or レベル<3 は PR を出さない（「無理して探さない」方針）。
"""

import os
import sys
import json
import re
import subprocess
import logging
from pathlib import Path
from typing import Optional

# Google GenAI SDK (Vertex AI)
from google import genai
# types は dict でも渡せるが、存在すれば利用可
try:
    from google.genai import types  # noqa: F401
except Exception:  # 古い版でも dict で代替可
    types = None  # type: ignore

# --- 既定/設定 ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(__file__).resolve().parents[2]

# 既存ワークフロー互換の環境変数
OUTPUT_FILE = os.getenv('LLM_REVIEW_OUTPUT', 'llm_review_result.txt')  # 使わないが互換維持
DEFAULT_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-pro')
LLM_INPUT_MODE = os.getenv('LLM_INPUT_MODE', 'full').strip().lower()  # 'full' / 'patch'
DIFF_CONTEXT = os.getenv('DIFF_CONTEXT', '3')

EXCLUDED_DIRS = {'node_modules', 'vendor', '.github', '.git', "dist", "build"}

# --- ロギング ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- ユーティリティ ---
def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, encoding='utf-8')


def ensure_history():
    """fetch-depth=1でも履歴が必要な Git 操作に耐える"""
    _ = run_cmd(['git', 'fetch', '--prune', '--unshallow', 'origin'])
    if _.returncode != 0:
        run_cmd(['git', 'fetch', '--prune', 'origin'])


def _default_remote_head() -> Optional[str]:
    r = run_cmd(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('/')[-1]
    return None


def _remote_branch_exists(name: str) -> bool:
    out = run_cmd(['git', 'ls-remote', '--heads', 'origin', name])
    return out.returncode == 0 and bool(out.stdout.strip())


def find_parent_branch(fallback_candidates: list[str] | None = None) -> str:
    """
    親ブランチ候補を決定（既存スクリプト互換）。
    1) workflow_dispatch 入力（BASE_BRANCH）で指定されていれば最優先
    2) develop -> main -> master
    3) origin/HEAD
    """
    base_env = os.getenv('BASE_BRANCH')  # workflow でセット
    if base_env and _remote_branch_exists(base_env):
        return base_env

    candidates = list(fallback_candidates or [])
    if not candidates:
        candidates = ['develop', 'main', 'master']
    default_head = _default_remote_head()
    if default_head and default_head not in candidates:
        candidates.append(default_head)

    ensure_history()
    run_cmd(['git', 'fetch', '--quiet', 'origin'])
    for c in candidates:
        if _remote_branch_exists(c):
            return c
    raise RuntimeError("親ブランチを特定できませんでした（develop/main/master または origin/HEAD が見つかりません）。")


def resolve_after_sha() -> str:
    """
    比較対象の最新（after）SHA を決定。
    - pull_request イベント: payload.pull_request.head.sha を優先
    - それ以外: GITHUB_SHA（なければ HEAD）
    """
    payload_path = os.getenv("GITHUB_EVENT_PATH")
    if payload_path and os.path.exists(payload_path):
        try:
            with open(payload_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            pr = payload.get('pull_request')
            if pr:
                head = (pr.get('head') or {}).get('sha')
                if head:
                    return head
        except Exception:
            pass
    return os.getenv('GITHUB_SHA', 'HEAD')


def merge_base_with_parent(parent_branch: str, after: str = "HEAD") -> str:
    mb = run_cmd(['git', 'merge-base', after, f'origin/{parent_branch}'])
    if mb.returncode != 0 or not mb.stdout.strip():
        raise RuntimeError(f"親ブランチ '{parent_branch}' とのマージベース取得に失敗: {mb.stderr}")
    return mb.stdout.strip()


def get_changed_files_between(merge_base: str, after: str = "HEAD") -> list[str]:
    diff = run_cmd(['git', 'diff', '--name-only', f'{merge_base}..{after}'])
    if diff.returncode != 0:
        raise RuntimeError(f"変更ファイルリストの取得に失敗: {diff.stderr}")
    files = [ln for ln in diff.stdout.strip().split('\n') if ln]
    # 除外ディレクトリをフィルタ
    filtered = []
    for p in files:
        parts = Path(p).parts
        if any(seg in EXCLUDED_DIRS for seg in parts):
            continue
        filtered.append(p)
    return filtered


def get_file_content_at_commit(sha: str, file_path: str) -> str:
    result = run_cmd(['git', 'show', f'{sha}:{file_path}'])
    if result.returncode == 0:
        return result.stdout
    logging.warning(f"コミット '{sha}' にファイル '{file_path}' が見つかりません。")
    return ""


def get_diff_patch(merge_base: str, after: str = "HEAD") -> str:
    context = DIFF_CONTEXT if DIFF_CONTEXT.isdigit() else '3'
    cmd = ["git", "diff", f"--unified={context}", f"{merge_base}..{after}"]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"diff取得に失敗: {result.stderr}")
    return result.stdout


def build_client() -> genai.Client:
    """
    GenAI SDK クライアント（Vertex AI/ADC）。環境変数から設定を取得。
    - GOOGLE_GENAI_USE_VERTEXAI=true
    - GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION
    """
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("環境変数 GOOGLE_CLOUD_PROJECT が未設定です。")
    # Vertex AI バックエンドで初期化（ADC）
    # SDKの使い方は公式PyPIを参照。:contentReference[oaicite:7]{index=7}
    return genai.Client(vertexai=True, project=project, location=location)


def call_gemini_for_patch(model_name: str, prompt_text: str) -> dict:
    """
    構造化JSON（application/json）でパッチ提案を受け取る。
    response_schema: decision/level/rationale/title/diff/notes
    - structured output は公式ドキュメントに準拠。:contentReference[oaicite:8]{index=8}
    """
    client = build_client()
    cfg = {
        "response_mime_type": "application/json",
        "temperature": 0.2,
        "max_output_tokens": 8192,
        "response_schema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["NO_CHANGES", "CHANGES"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 5},
                "rationale": {"type": "string"},
                "title": {"type": "string"},
                "diff": {"type": "string"},
                "notes": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["decision", "level", "rationale", "title", "diff"],
            "additionalProperties": False
        }
    }
    resp = client.models.generate_content(
        model=model_name,
        contents=prompt_text,
        config=cfg,
    )
    text = getattr(resp, "text", None)
    if not text or not str(text).strip():
        raise RuntimeError("LLMからの応答が空です。")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON のパースに失敗: {e}\nraw={text[:500]}...")


def make_prompt(review_content: str) -> str:
    """
    LLM への最終プロンプト（JSONのみ返すこと、unified diff の厳密条件、無理に修正点を探さない 等）。
    """
    return f"""
あなたは厳格なコードレビュア兼リファクタ提案者です。以下を厳守し、**JSONのみ**で出力してください。

- 目的：**本当に必要な**最小限のリファクタのみを提案する。
- 「無理して修正点を探さない」。可読性・安全性・保守性・パフォーマンスの観点で**明確な根拠**がある場合のみ変更する。
- 変更が不要なら `"decision": "NO_CHANGES"`。
- 変更が必要なら以下を返す：
  - `"level"`：1〜5（5=強い必要性）。**1未満**は提案対象外。
  - `"rationale"`：「なぜこの修正を行うのか」を一文で明確に（PR本文にそのまま使用）。
  - `"title"`：わかりやすいPRタイトル。
  - `"diff"`：**現在のHEADに適用可能な unified diff**（`git apply --index` で適用可、`--unified=3`相当の文脈行を含む、`diff --git` から開始、不要な出力禁止）。
  - `"notes"`：補足（任意、箇条書き）。

制約：
- ビルド成果物/依存物（node_modules/vendor/.github 等）は変更対象外。
- 大規模改変やリネームは避け、**安全サイド**で最小差分にする。
- コーディング規約の微調整のみなど**必要性が低い**変更は提案しない。

# コンテキスト（解析対象）
{review_content}
    """.strip()


def gather_full_contents(after_sha: str, files: list[str], byte_budget: int = 400_000) -> str:
    """
    'full' モード：指定ファイルの内容をまとめる。合計 ~400KB 程度に制限。
    バイナリ/巨大ファイルはスキップ。
    """
    binary_ext = {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.so', '.exe', '.dll'}
    total = 0
    blocks: list[str] = []
    for fp in files:
        p = Path(fp)
        if p.suffix.lower() in binary_ext:
            continue
        if any(seg in EXCLUDED_DIRS for seg in p.parts):
            continue
        # HEAD の内容
        text = get_file_content_at_commit(after_sha, fp)
        if not text:
            continue
        chunk = f"--- ファイル: {fp} ---\n```\n{text}\n```\n"
        inc = len(chunk.encode('utf-8'))
        if total + inc > byte_budget:
            break
        blocks.append(chunk)
        total += inc
    if not blocks:
        return "（対象ファイルの有効なテキストが見つかりませんでした）"
    return "\n".join(blocks)


def apply_patch_and_commit(diff_text: str, level: int, base_branch: str) -> Optional[str]:
    """
    diff を適用→コミット。成功したら新規ブランチ名を返す。失敗時は None。
    """
    new_branch = f"auto/refactor-{os.getpid()}"
    # ベースブランチから作業ブランチ
    _ = run_cmd(['git', 'checkout', base_branch]); _ = run_cmd(['git', 'pull', '--ff-only'])
    _ = run_cmd(['git', 'checkout', '-b', new_branch])

    # unified diff を index に適用（reject/whitespaceもケア）
    # git apply の使用は公式リファレンス/一般解説に準拠。:contentReference[oaicite:9]{index=9}
    p = subprocess.Popen(["git", "apply", "--index", "--reject", "--whitespace=fix", "-"],
                         stdin=subprocess.PIPE, text=True)
    assert p.stdin is not None
    p.stdin.write(diff_text); p.stdin.close()
    rc = p.wait()
    if rc != 0:
        logging.error("git apply に失敗しました")
        return None

    # 変更が無ければ中止
    chk = run_cmd(['bash', '-lc', 'git diff --cached --quiet || echo CHANGES'])
    if chk.stdout.strip() != "CHANGES":
        logging.info("適用後のステージ変更がありません。PRは作成しません。")
        return None

    msg = f"chore(refactor): apply Gemini suggestions (level={level})"
    _ = run_cmd(['git', 'commit', '-m', msg])
    return new_branch


def validate_unified_diff(diff_text: str) -> bool:
    return bool(re.match(r'^(diff --git|--- |\+\+\+ )', diff_text))


def main() -> None:
    model = DEFAULT_MODEL
    try:
        logging.info("親ブランチを探索し、マージベースから変更を抽出しています...")
        parent = find_parent_branch()
        after_sha = resolve_after_sha()
        merge_base = merge_base_with_parent(parent, after=after_sha)

        changed_files = get_changed_files_between(merge_base, after_sha)
        logging.info(f"親ブランチ: {parent} / マージベース: {merge_base}")
        logging.info(f"変更されたファイル数: {len(changed_files)}")

        # 入力モードに応じて LLM コンテキストを作成
        if LLM_INPUT_MODE == 'patch':
            patch_text = get_diff_patch(merge_base, after_sha)
            review_target = f"--- Unified Diff (context={DIFF_CONTEXT}) ---\n```\n{patch_text}\n```\n"
        else:
            # 'full'：変更ファイルの全文（サイズ制限）
            review_target = gather_full_contents(after_sha, changed_files)

        prompt = make_prompt(review_target)
        logging.info(f"Gemini モデル '{model}' でリファクタ提案を生成します（Vertex/ADC）...")
        result = call_gemini_for_patch(model, prompt)
        logging.info("LLM 応答を受信しました。")

        decision = (result.get("decision") or "").upper()
        if decision == "NO_CHANGES":
            logging.info("変更不要（NO_CHANGES）。PRは作成しません。")
            print("NEED_PR=0", file=open(os.environ["GITHUB_ENV"], "a"))
            return

        level = int(result.get("level") or 0)
        if level < 3:
            logging.info(f"リファクタレベル={level} (<3)。PRは作成しません。")
            print("NEED_PR=0", file=open(os.environ["GITHUB_ENV"], "a"))
            return

        diff_text = (result.get("diff") or "").strip()
        title = result.get("title", "Refactor: improvements")
        rationale = (result.get("rationale") or "Reason not provided").strip()

        if not diff_text or not validate_unified_diff(diff_text):
            logging.warning("LLMの diff が空/不正のため、PRは作成しません。")
            print("NEED_PR=0", file=open(os.environ["GITHUB_ENV"], "a"))
            return

        new_branch = apply_patch_and_commit(diff_text, level, base_branch=parent)
        if not new_branch:
            print("NEED_PR=0", file=open(os.environ["GITHUB_ENV"], "a"))
            return

        # PR 本文（「なぜ」と「レベル」を必ず記載）
        pr_body = f"""### 目的（なぜこの修正を行ったのか）
{rationale}

### リファクタレベル
**{level} / 5**

> 本PRは Gemini 2.5 Pro による自動レビュー結果に基づく提案です。
> 「無理して修正点を探さない」方針で、必要性が低い場合は PR を作成していません。
"""

        # 後段ステップへ環境変数で受け渡し
        envf = os.environ.get("GITHUB_ENV")
        assert envf, "GITHUB_ENV が見つかりません"
        with open(envf, "a", encoding="utf-8") as f:
            f.write("NEED_PR=1\n")
            f.write(f"NEW_BRANCH={new_branch}\n")
            f.write(f"PR_TITLE={title}\n")
            f.write("PR_BODY<<__EOF__\n"); f.write(pr_body); f.write("\n__EOF__\n")

        logging.info(f"PR準備完了（ブランチ: {new_branch}）。後段で gh pr create を実行します。")

    except (ValueError, RuntimeError) as e:
        logging.error(f"処理が中断されました: {e}")
        # 認証状態を補助ログとして出力
        try:
            # GenAI SDK には auth status コマンドは無いが、環境を出力
            logging.error(f"ENV: PROJECT={os.getenv('GOOGLE_CLOUD_PROJECT')} LOCATION={os.getenv('GOOGLE_CLOUD_LOCATION')}")
        except Exception:
            pass
        # PRは作らない
        envf = os.environ.get("GITHUB_ENV")
        if envf:
            print("NEED_PR=0", file=open(envf, "a"))
        sys.exit(0)
    except Exception as e:
        logging.error(f"予期せぬエラー: {e}")
        envf = os.environ.get("GITHUB_ENV")
        if envf:
            print("NEED_PR=0", file=open(envf, "a"))
        sys.exit(0)


if __name__ == '__main__':
    main()
