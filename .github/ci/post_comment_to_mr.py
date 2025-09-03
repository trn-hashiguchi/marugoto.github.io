#!/usr/bin/env python3
# -*- coding: utf-8 -*-import os
import sys
import json
import requests
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def load_pr_number_from_event() -> int | None:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None
    try:
        with open(event_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("number") or (payload.get("pull_request") or {}).get("number")
    except Exception as e:
        logging.error(f"イベントペイロードの読み込みに失敗: {e}")
        return None

def find_suggestion_pr_url(head_ref: str) -> str | None:
    """APIを叩いて、提案PRのURLを見つける"""
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    suggestion_branch = f"llm-fix/{head_ref}"
    
    api_url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    params = {"head": f"{repo.split('/')[0]}:{suggestion_branch}", "state": "open"}
    
    try:
        resp = requests.get(api_url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            prs = resp.json()
            if prs:
                return prs[0].get("html_url")
    except requests.RequestException as e:
        logging.warning(f"提案PRの検索中にエラー: {e}")
    return None


def format_comment(result: dict) -> str:
    """JSONから人間が読みやすいMarkdownコメントを生成する"""
    model_name = os.getenv("GEMINI_MODEL", "未指定")
    head_ref = os.getenv("GITHUB_HEAD_REF", "")

    # 修正理由
    rationale = result.get("rationale", "提供されていません。").strip()
    
    # 補足事項
    notes = result.get("notes", [])
    notes_md = ""
    if notes and isinstance(notes, list):
        notes_md = "\n\n#### 補足事項\n" + "\n".join(f"- {note}" for note in notes)

    # 提案PRへのリンク
    suggestion_pr_link = ""
    if os.getenv("SUGGESTION_PR_CREATED") == "true" and head_ref:
        url = find_suggestion_pr_url(head_ref)
        if url:
            suggestion_pr_link = f"\n\n---\n\n**[➡️ この修正案を適用する (Click here to apply this suggestion)]({url})**"

    comment = f"""🤖 **AIによる自動コードレビュー結果 (Model: {model_name})**

### 修正理由
{rationale}
{notes_md}
{suggestion_pr_link}
"""
    return comment.strip()


def post_comment_to_github(comment_body: str):
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    pr_number = load_pr_number_from_event()

    if not all([repo, token, pr_number]):
        logging.info("PRコンテキストでないか、認証情報が不足しているためスキップします。")
        return

    api_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    data = {"body": comment_body}

    try:
        resp = requests.post(api_url, headers=headers, json=data, timeout=30)
        if 200 <= resp.status_code < 300:
            logging.info(f"PR #{pr_number} へのコメント投稿に成功しました。")
        else:
            logging.error(f"コメント投稿に失敗: {resp.status_code} {resp.text}")
            sys.exit(1)
    except requests.RequestException as e:
        logging.error(f"コメント投稿中にエラー: {e}")
        sys.exit(1)

def read_review_json() -> dict:
    # download-artifactのパスを優先
    path = Path("artifacts/llm_review_result.json")
    if not path.exists():
        path = Path("llm_review_result.json")
    
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logging.error(f"JSONのパースに失敗: {e}")
            sys.exit(1)
            
    logging.error(f"レビュー結果ファイルが見つかりません: {path}")
    sys.exit(1)

if __name__ == "__main__":
    result_json = read_review_json()
    
    # 修正がない場合はコメントしない
    if result_json.get("decision", "").upper() == "NO_CHANGES":
        logging.info("修正提案がないため、コメント投稿をスキップします。")
        sys.exit(0)
        
    # 提案PRが作られたかどうかを後続ジョブから判断するための環境変数をセット
    # create_suggestion_pr ジョブが if 条件で実行されたかどうかで判断
    if os.getenv("NEEDS_CREATE_PR") == "true":
        os.environ["SUGGESTION_PR_CREATED"] = "true"

    comment = format_comment(result_json)
    post_comment_to_github(comment)