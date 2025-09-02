# .github/ci/post_comment_to_mr.py  （ファイル名はそのままでOK）
"""
LLMによるレビュー結果を読み込み、GitHubのPull Requestにコメントとして投稿するスクリプト。
- 実行環境: GitHub Actions（pull_request イベント）
- 認証: GITHUB_TOKEN（自動付与）
"""
import os
import sys
import json
import requests
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def load_pr_number_from_event() -> int | None:
    """
    GitHub Actions が提供するイベントペイロード（GITHUB_EVENT_PATH）から PR 番号を取得。
    pull_request イベントの場合、payload["number"] もしくは payload["pull_request"]["number"]。
    """
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        logging.info("GITHUB_EVENT_PATH が見つからないため、PR 番号の取得をスキップします。")
        return None

    try:
        with open(event_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        # 両方に対応
        return payload.get("number") or (payload.get("pull_request") or {}).get("number")
    except Exception as e:
        logging.error(f"イベントペイロードの読み込みに失敗しました: {e}")
        return None


def post_comment_to_github(review_content: str) -> None:
    """
    GitHub の Pull Request にコメントを投稿する。
    PR への一般コメントは Issues API（/issues/{issue_number}/comments）で行う。
    """
    repo = os.getenv("GITHUB_REPOSITORY")  # "owner/repo"
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")  # どちらでも可
    model_name = os.getenv("GEMINI_MODEL", "未指定")

    if not repo:
        logging.error("環境変数 'GITHUB_REPOSITORY' が設定されていません。")
        sys.exit(1)
    if not token:
        logging.error("環境変数 'GITHUB_TOKEN' が設定されていません。")
        sys.exit(1)

    pr_number = load_pr_number_from_event()
    if not pr_number:
        logging.info("Pull Request コンテキストではないため、コメント投稿をスキップします。")
        return

    owner, repository = repo.split("/", 1)
    api_url = f"https://api.github.com/repos/{owner}/{repository}/issues/{pr_number}/comments"

    comment_body = (
        f"🤖 **AIによる自動コードレビュー結果 (Model: {model_name})**\n\n"
        f"---\n\n{review_content}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # UA は任意だが明示しておくとデバッグしやすい
        "User-Agent": "llm-review-bot",
    }
    data = {"body": comment_body}

    try:
        resp = requests.post(api_url, headers=headers, json=data, timeout=30)
        if resp.status_code >= 200 and resp.status_code < 300:
            logging.info(f"PR #{pr_number} へのコメント投稿に成功しました。")
        else:
            logging.error("コメントの投稿に失敗しました。")
            logging.error(f"ステータスコード: {resp.status_code}")
            logging.error(f"レスポンス: {resp.text}")
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        logging.error(f"エラー: コメントの投稿に失敗しました。詳細: {e}")
        sys.exit(1)


def read_review_text() -> str:
    """
    レビュー結果ファイルを読み込む。
    既定: カレントの 'llm_review_result.txt'
    予備: './artifacts/llm_review_result.txt'（Actions の download-artifact 既定に合わせる）
    環境変数 REVIEW_FILE があればそれを優先。
    """
    candidates = [
        os.getenv("REVIEW_FILE", "llm_review_result.txt"),
        os.path.join("artifacts", "llm_review_result.txt"),
    ]
    for path in candidates:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    logging.error(f"エラー: レビュー結果ファイルが見つかりません。探索パス: {candidates}")
    sys.exit(1)


if __name__ == "__main__":
    review_text = read_review_text()
    post_comment_to_github(review_text)
