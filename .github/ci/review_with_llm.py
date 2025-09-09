#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GitHub Actions で動作する LLM コードレビュー用スクリプト（ADC/Vertex AI）。
- PR のベースブランチを自動特定し、merge-base..HEAD（または PR の head.sha）を対象にレビュー
- LLM へ渡す内容は「全文」(default) と「パッチ(diff)」を環境変数で切り替え
- パッチの前後行数（コンテキスト）は環境変数で制御
- .github/ci/file_list_prompt.md に記載されたファイルを前提知識として読み込み

前提:
- サービスアカウントに Vertex AI ユーザー権限 (roles/aiplatform.user)
- ADC: export GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/sa-key.json
- 必要に応じて:
    export GEMINI_MODEL=gemini-2.5-pro-preview-03-25

主な環境変数:
- GEMINI_MODEL         : 利用するモデルID（省略時: gemini-2.5-pro-preview-03-25）
- LLM_REVIEW_OUTPUT    : 出力ファイル（省略時: llm_review_result.txt）
- LLM_INPUT_MODE       : 'full'（変更ファイルの全文）/ 'patch'（差分パッチ）。省略時: 'full'
- DIFF_CONTEXT         : 'patch'モード時の前後行数。省略時: '3'
- GOOGLE_CLOUD_PROJECT : Vertex AI 用 GCP プロジェクト（必須）
- GOOGLE_CLOUD_LOCATION: Vertex AI ロケーション（省略時: global）

GitHub 変数:
- GITHUB_EVENT_PATH  : イベントペイロード（JSON）
- GITHUB_BASE_REF    : PR のベースブランチ（pull_request イベント）
- GITHUB_HEAD_REF    : PR のヘッドブランチ名
- GITHUB_SHA         : 実行対象の SHA
- GITHUB_REPOSITORY  : "owner/repo"
"""

import os
import sys
import json
import subprocess
import logging
from pathlib import Path
from typing import Optional

# Google Gen AI SDK
from google import genai

# --- 定数 ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(SCRIPT_DIR, 'prompt.md')
FILE_LIST_PROMPT_PATH = os.path.join(SCRIPT_DIR, 'file_list_prompt.md')

OUTPUT_FILE = os.getenv('LLM_REVIEW_OUTPUT', 'llm_review_result.txt')
DEFAULT_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-pro-preview-03-25')
LLM_INPUT_MODE = os.getenv('LLM_INPUT_MODE', 'full').strip().lower()
DIFF_CONTEXT = os.getenv('DIFF_CONTEXT', '3')

DEFAULT_PROMPT = """あなたは経験豊富なシニアソフトウェアエンジニアとして、コードレビューを行います。
以下の観点に沿って、具体的で実践的な指摘をしてください。

- **バグの可能性**: 潜在的なバグやエッジケースの見落としがないか。
- **可読性**: 変数名や関数名が適切か、コメントは分かりやすいか。
- **パフォーマンス**: 非効率な処理や、より高速な代替手段がないか。
- **セキュリティ**: 脆弱性（例: XSS、SQLインジェクション）がないか。
- **ベストプラクティス**: 一般的な設計原則やコーディング規約に従っているか。

出力フォーマット:
1) 要約
2) 重要度の高い指摘（番号付き）
3) 改善案（diff形式の修正例付き）
4) 追加のテスト観点
5) リスクとロールバック戦略
"""

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- ユーティリティ ---
def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, encoding='utf-8')


def get_repo_root() -> str:
    """リポジトリのルートディレクトリの絶対パスを取得する"""
    try:
        result = run_cmd(['git', 'rev-parse', '--show-toplevel'])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        logging.warning(f"git rev-parse の実行に失敗: {e}。カレントディレクトリをルートとみなします。")
    return os.getcwd()


def load_system_prompt() -> str:
    try:
        with open(PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        logging.warning(f"プロンプトファイル '{PROMPT_PATH}' が見つかりません。デフォルトのプロンプトを使用します。")
        return DEFAULT_PROMPT


def load_prerequisite_paths() -> list[str]:
    """前提条件ファイルリストを読み込んでパスのリストを返す"""
    if not os.path.exists(FILE_LIST_PROMPT_PATH):
        logging.info(f"前提条件ファイルリスト '{FILE_LIST_PROMPT_PATH}' が見つかりません。")
        return []
    
    paths = []
    with open(FILE_LIST_PROMPT_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            paths.append(line)
    return paths


def load_prerequisite_content(repo_root: str, paths: list[str]) -> str:
    """指定されたパスのファイル/ディレクトリからコンテンツを読み込む"""
    content = ""
    root_path = Path(repo_root)
    binary_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.so', '.exe', '.dll', '.DS_Store'}

    for rel_path_str in paths:
        path = root_path / rel_path_str
        if not path.exists():
            logging.warning(f"前提知識ファイル/ディレクトリが見つかりません: '{rel_path_str}'")
            continue

        files_to_read = []
        if path.is_dir():
            files_to_read.extend(sorted(p for p in path.rglob('*') if p.is_file()))
        elif path.is_file():
            files_to_read.append(path)

        for file_path in files_to_read:
            if any(file_path.name.lower().endswith(ext) for ext in binary_extensions):
                continue
            try:
                relative_file_path = file_path.relative_to(root_path)
                content += f"--- 前提ファイル: {relative_file_path} ---\n```\n"
                content += file_path.read_text(encoding='utf-8')
                content += "\n```\n\n"
            except UnicodeDecodeError:
                logging.warning(f"ファイル '{file_path}' はUTF-8でデコードできませんでした。スキップします。")
            except Exception as e:
                logging.warning(f"ファイル '{file_path}' の読み込みに失敗: {e}")
    
    return content


def load_event_payload() -> dict:
    """GITHUB_EVENT_PATH から JSON ペイロードを読み込む（存在しない場合は空）"""
    path = os.getenv("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"イベントペイロードの読み込みに失敗: {e}")
        return {}


def ensure_history():
    """
    actions/checkout の既定 fetch-depth=1 でも動くように履歴を確保。
    - 失敗したら通常の fetch にフォールバック
    """
    _ = run_cmd(['git', 'fetch', '--prune', '--unshallow', 'origin'])
    if _.returncode != 0:
        run_cmd(['git', 'fetch', '--prune', 'origin'])


# --- 親ブランチの特定と差分（GitHub対応） ---
def _default_remote_head() -> Optional[str]:
    r = run_cmd(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'])
    if r.returncode == 0 and r.stdout.strip():
        ref = r.stdout.strip()
        return ref.split('/')[-1]
    return None


def _remote_branch_exists(name: str) -> bool:
    out = run_cmd(['git', 'ls-remote', '--heads', 'origin', name])
    return out.returncode == 0 and bool(out.stdout.strip())

def find_parent_branch() -> str:
    """
    親ブランチ候補を決定する（GitHub版）。
    1) pull_request イベントなら GITHUB_BASE_REF
    2) develop -> main -> master
    3) origin/HEAD が指す既定ブランチ
    """
    payload = load_event_payload()
    base_from_payload = None
    if 'pull_request' in payload:
        base_from_payload = (payload['pull_request'].get('base') or {}).get('ref')

    base_env = os.getenv('GITHUB_BASE_REF')  # pull_request イベント
    mr_target = base_from_payload or base_env

    candidates: list[str] = []
    if mr_target:
        candidates.append(mr_target)
    candidates += ['develop', 'main', 'master']
    default_head = _default_remote_head()
    if default_head and default_head not in candidates:
        candidates.append(default_head)

    ensure_history()
    run_cmd(['git', 'fetch', '--quiet', 'origin'])

    for c in candidates:
        if _remote_branch_exists(c):
            return c
    raise RuntimeError("親ブランチを特定できませんでした（develop/main/master または origin/HEAD が見つかりません）。")


def merge_base_with_parent(parent_branch: str, after: str = "HEAD") -> str:
    mb = run_cmd(['git', 'merge-base', after, f'origin/{parent_branch}'])
    if mb.returncode != 0 or not mb.stdout.strip():
        raise RuntimeError(f"親ブランチ '{parent_branch}' とのマージベース取得に失敗: {mb.stderr}")
    return mb.stdout.strip()

def get_changed_files_between(merge_base: str, after: str = "HEAD") -> list[str]:
    diff = run_cmd(['git', 'diff', '--name-only', f'{merge_base}..{after}'])
    if diff.returncode != 0:
        raise RuntimeError(f"変更ファイルリストの取得に失敗しました: {diff.stderr}")
    return [ln for ln in diff.stdout.strip().split('\n') if ln]

def get_file_content_at_commit(sha: str, file_path: str) -> str:
    result = run_cmd(['git', 'show', f'{sha}:{file_path}'])
    if result.returncode == 0:
        return result.stdout
    logging.warning(f"コミット '{sha}' にファイル '{file_path}' が見つかりませんでした。")
    return ""

def get_diff_patch(merge_base: str, after: str = "HEAD") -> str:
    context = DIFF_CONTEXT if DIFF_CONTEXT.isdigit() else '3'
    cmd = ["git", "diff", f"--unified={context}", f"{merge_base}..{after}"]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"diff取得に失敗しました: {result.stderr}")
    return result.stdout

def resolve_after_sha() -> str:
    """
    差分の「後ろ側」SHA（比較対象の最新）を決定。
    - pull_request イベント: payload.pull_request.head.sha を優先
    - それ以外: 環境変数 GITHUB_SHA（なければ 'HEAD'）
    """
    payload = load_event_payload()
    pr = payload.get('pull_request') if payload else None
    if pr:
        head = (pr.get('head') or {}).get('sha')
        if head:
            return head
    return os.getenv('GITHUB_SHA', 'HEAD')


# --- Gemini 呼び出し（ADC / Vertex AI） ---
def build_client() -> genai.Client:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    if not project:
        raise RuntimeError("環境変数 GOOGLE_CLOUD_PROJECT が未設定です。")
    return genai.Client(vertexai=True, project=project, location=location)

def review_code_with_gemini_adc(model_name: str, review_content: str, prerequisite_content: str = "") -> str:
    system_prompt = load_system_prompt()
    
    prerequisite_section = ""
    if prerequisite_content:
        prerequisite_section = f"""---
## 前提知識
レビューを行う前に、以下のファイルの内容をコンテキストとして完全に理解してください。
これらは、プロジェクトの設計思想、コーディング規約、またはレビュー対象のコードが依存する重要なモジュールに関する情報を含んでいます。

{prerequisite_content}---
"""

    user_prompt = f"""{prerequisite_section}
## レビュー依頼
以下のファイル群または差分について、シニアソフトウェアエンジニアとしてコードレビューを実施してください。

{review_content}
"""
    contents = f"{system_prompt}\n\n{user_prompt}"

    client = build_client()
    try:
        resp = client.models.generate_content(model=model_name, contents=contents)
        text = getattr(resp, "text", None)
        if not text or not str(text).strip():
            raise RuntimeError("LLMからの応答にレビュー結果が含まれていませんでした。")
        return text.strip()
    except Exception as e:
        raise RuntimeError(f"LLMによるレビュー中にエラーが発生しました: {e}") from e


# --- メイン処理 ---
def main() -> None:
    output_file = OUTPUT_FILE
    model = DEFAULT_MODEL

    try:
        repo_root = get_repo_root()

        logging.info("前提知識ファイルを読み込んでいます...")
        prerequisite_paths = load_prerequisite_paths()
        prerequisite_content = ""
        if prerequisite_paths:
            prerequisite_content = load_prerequisite_content(repo_root, prerequisite_paths)
            if prerequisite_content:
                logging.info(f"{len(prerequisite_paths)}個のパス指定から前提知識を読み込みました。")

        logging.info("親ブランチを探索し、マージベースから変更を抽出しています...")
        parent = find_parent_branch()
        after_sha = resolve_after_sha()
        merge_base = merge_base_with_parent(parent, after=after_sha)

        changed_files = get_changed_files_between(merge_base, after_sha)

        # バイナリ拡張子は除外
        binary_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.so', '.exe', '.dll'}
        text_files = [f for f in changed_files if not any(f.lower().endswith(ext) for ext in binary_extensions)]

        if not changed_files:
            result_message = "コードの変更が検出されなかったため、レビューをスキップしました。"
            logging.info(result_message)
        else:
            logging.info(f"親ブランチ: {parent} / マージベース: {merge_base}")
            logging.info(f"変更されたファイル: {', '.join(changed_files)}")

            if LLM_INPUT_MODE == 'patch':
                patch_text = get_diff_patch(merge_base, after_sha)
                review_target_content = f"--- Unified Diff (context={DIFF_CONTEXT}) ---\n```\n{patch_text}\n```\n"
            else:
                review_target_content = ""
                for file_path in text_files:
                    content_after = get_file_content_at_commit(after_sha, file_path)
                    review_target_content += f"--- ファイル: {file_path} ---\n```\n{content_after}\n```\n\n"

            logging.info(f"Gemini モデル '{model}' によるコードレビューを開始します（ADC）...")
            review_result = review_code_with_gemini_adc(model, review_target_content, prerequisite_content)
            logging.info("レビューが完了しました。")
            result_message = review_result

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(result_message)
        logging.info(f"レビュー結果を '{output_file}' に保存しました。")

    except (ValueError, RuntimeError) as e:
        logging.error(f"処理が中断されました: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"予期せぬエラーが発生しました: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
