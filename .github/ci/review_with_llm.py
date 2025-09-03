#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import re
import subprocess
import logging
from pathlib import Path
from typing import Optional

from google import genai

# --- 設定 ---
# 出力ファイル名はJSONに変更
OUTPUT_FILE = os.getenv('LLM_REVIEW_OUTPUT', 'llm_review_result.json')
DEFAULT_MODEL = os.getenv('GEMINI_MODEL', 'gemini-1.5-pro-latest')
LLM_INPUT_MODE = os.getenv('LLM_INPUT_MODE', 'full').strip().lower()
DIFF_CONTEXT = os.getenv('DIFF_CONTEXT', '3')
EXCLUDED_DIRS = {'node_modules', 'vendor', '.git', "dist", "build"} # .github はレビュー対象に含める

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ユーティリティ (gemini_refactor.py から移植) ---
def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, encoding='utf-8')

def ensure_history():
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

def find_parent_branch() -> str:
    # PRイベントの GITHUB_BASE_REF を最優先
    base_env = os.getenv('GITHUB_BASE_REF')
    if base_env and _remote_branch_exists(base_env):
        return base_env

    candidates = ['develop', 'main', 'master']
    default_head = _default_remote_head()
    if default_head and default_head not in candidates:
        candidates.append(default_head)

    ensure_history()
    run_cmd(['git', 'fetch', '--quiet', 'origin'])
    for c in candidates:
        if _remote_branch_exists(c):
            return c
    raise RuntimeError("親ブランチを特定できませんでした。")

def resolve_after_sha() -> str:
    payload_path = os.getenv("GITHUB_EVENT_PATH")
    if payload_path and os.path.exists(payload_path):
        try:
            with open(payload_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            pr = payload.get('pull_request')
            if pr:
                head = (pr.get('head') or {}).get('sha')
                if head: return head
        except Exception: pass
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
    return [p for p in files if not any(seg in EXCLUDED_DIRS for seg in Path(p).parts)]

def get_file_content_at_commit(sha: str, file_path: str) -> str:
    result = run_cmd(['git', 'show', f'{sha}:{file_path}'])
    return result.stdout if result.returncode == 0 else ""

def get_diff_patch(merge_base: str, after: str = "HEAD") -> str:
    context = DIFF_CONTEXT if DIFF_CONTEXT.isdigit() else '3'
    cmd = ["git", "diff", f"--unified={context}", f"{merge_base}..{after}"]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"diff取得に失敗: {result.stderr}")
    return result.stdout

def gather_full_contents(after_sha: str, files: list[str], byte_budget: int = 400_000) -> str:
    binary_ext = {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.so', '.exe', '.dll'}
    total = 0
    blocks: list[str] = []
    for fp in files:
        p = Path(fp)
        if p.suffix.lower() in binary_ext: continue
        text = get_file_content_at_commit(after_sha, fp)
        if not text: continue
        chunk = f"--- ファイル: {fp} ---\n```\n{text}\n```\n"
        inc = len(chunk.encode('utf-8'))
        if total + inc > byte_budget: break
        blocks.append(chunk)
        total += inc
    return "\n".join(blocks) if blocks else "（対象ファイルの有効なテキストが見つかりませんでした）"

# --- Gemini 呼び出し (gemini_refactor.py から移植・改造) ---
def build_client() -> genai.Client:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("環境変数 GOOGLE_CLOUD_PROJECT が未設定です。")
    return genai.Client(vertexai=True, project=project, location=location)

def make_prompt(review_content: str) -> str:
    return f"""
あなたはシニアエンジニアとしてコードレビューを行います。以下を厳守し、**JSONのみ**で出力してください。

- 目的：コード品質を向上させるための、具体的で実践的な指摘を行う。
- 指摘が不要な場合：`"decision": "NO_CHANGES"` を返す。
- 指摘が必要な場合：
  - `"decision": "CHANGES"` を返す。
  - `"rationale"`：**なぜこの修正が必要か**をレビュイーに伝わるように、簡潔かつ丁寧に説明する。
  - `"diff"`：**HEADに適用可能な unified diff** を生成する (`git apply`可能な形式)。
  - `"notes"`：その他の補足事項（任意）。

制約：
- 大規模なリファクタリングは避け、PRの範囲に沿った最小限の修正に留める。
- コーディングスタイルの些細な修正（インデント等）は指摘しない。
- バグの可能性、セキュリティ、パフォーマンス、可読性の観点で明確なメリットがある場合にのみ指摘する。

# レビュー対象
{review_content}
    """.strip()

def call_gemini_for_review(model_name: str, prompt_text: str) -> dict:
    client = build_client()
    cfg = {
        "response_mime_type": "application/json",
        "temperature": 0.1,
        "max_output_tokens": 8192,
        "response_schema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["NO_CHANGES", "CHANGES"]},
                "rationale": {"type": "string"},
                "diff": {"type": "string"},
                "notes": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["decision", "rationale", "diff"],
        }
    }
    resp = client.models.generate_content(model=model_name, contents=prompt_text, config=cfg)
    text = getattr(resp, "text", None)
    if not text or not str(text).strip():
        raise RuntimeError("LLMからの応答が空です。")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON のパースに失敗: {e}\nraw={text[:500]}...")

def validate_unified_diff(diff_text: str) -> bool:
    return bool(re.match(r'^(diff --git|--- |\+\+\+ )', diff_text))

# --- メイン処理 ---
def main() -> None:
    try:
        parent = find_parent_branch()
        after_sha = resolve_after_sha()
        merge_base = merge_base_with_parent(parent, after=after_sha)
        changed_files = get_changed_files_between(merge_base, after_sha)

        if not changed_files:
            logging.info("コードの変更が検出されなかったため、レビューをスキップしました。")
            result_json = {"decision": "NO_CHANGES", "rationale": "変更ファイルなし", "diff": "", "notes": []}
        else:
            logging.info(f"親ブランチ: {parent}, 変更ファイル数: {len(changed_files)}")
            if LLM_INPUT_MODE == 'patch':
                review_target = get_diff_patch(merge_base, after_sha)
            else:
                review_target = gather_full_contents(after_sha, changed_files)

            prompt = make_prompt(review_target)
            logging.info(f"Gemini モデル '{DEFAULT_MODEL}' でレビューと修正案生成を開始します...")
            result_json = call_gemini_for_review(DEFAULT_MODEL, prompt)
            logging.info("LLM 応答を受信しました。")

        # 結果をJSONファイルに保存
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(result_json, f, ensure_ascii=False, indent=2)
        logging.info(f"レビュー結果を '{OUTPUT_FILE}' に保存しました。")

        # 後続ジョブのために GITHUB_OUTPUT に結果を書き込む
        decision = result_json.get("decision", "NO_CHANGES").upper()
        diff_text = result_json.get("diff", "").strip()
        
        needs_pr = "0"
        if decision == "CHANGES" and diff_text and validate_unified_diff(diff_text):
            needs_pr = "1"
            logging.info("有効な修正案が見つかりました。PR作成を要求します。")
        else:
            logging.info("修正案がないか、diffが不正なためPRは作成しません。")

        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"needs_pr={needs_pr}\n")

    except Exception as e:
        logging.error(f"処理が中断されました: {e}")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("needs_pr=0\n")
        sys.exit(1)

if __name__ == '__main__':
    main()