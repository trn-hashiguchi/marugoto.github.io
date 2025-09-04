#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM refactor orchestrator (per-file).
- modes:
  - prepare: docs/check.md と docs/checklists/** の初期化と、今回処理するファイル一覧(JSON)を GITHUB_OUTPUT へ返す
  - refactor-single: 単一ファイルを LLM でリファクタし、checklist を更新、PR本文を生成。変更の有無等を GITHUB_OUTPUT へ返す
"""

import os, sys, re, json, hashlib, argparse, subprocess
from pathlib import Path
from typing import List

# Google GenAI SDK
from google import genai
from google.genai import types as genai_types

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
CHECK_INDEX = DOCS_DIR / "check.md"
CHECK_DIR = DOCS_DIR / "checklists"

DEFAULT_INCLUDE = [
    "src/**","app/**","lib/**",
    "**/*.py","**/*.js","**/*.ts","**/*.tsx",
    "**/*.php","**/*.go","**/*.java","**/*.cs",
    "**/*.rb","**/*.rs","**/*.kt","**/*.swift",
]
DEFAULT_EXCLUDE = [
    ".git/**",".github/**","build/**","dist/**","out/**","node_modules/**","vendor/**",
    "public/**","coverage/**","**/*.min.js","**/*.map","**/*.lock",
    "**/*.png","**/*.jpg","**/*.jpeg","**/*.gif","**/*.svg","**/*.pdf",
    "package.json","package-lock.json","yarn.lock","pnpm-lock.yaml",
    "docs/checklists/**","docs/check.md",
]

def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)

def set_output(key: str, value: str):
    fn = os.environ.get("GITHUB_OUTPUT")
    if not fn:
        print(f"[WARN] GITHUB_OUTPUT not set. {key}={value[:120]}")
        return
    with open(fn, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")

def git_ls_files() -> List[str]:
    r = run(["git", "ls-files"])
    r.check_returncode() if r.returncode == 0 else None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

def _matches(path: str, patterns: List[str]) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(path, pat) for pat in patterns)

def pick_candidates(include_globs: List[str], exclude_globs: List[str]) -> List[str]:
    files = git_ls_files()
    out: List[str] = []
    for f in files:
        if _matches(f, exclude_globs):
            continue
        if include_globs and not _matches(f, include_globs):
            continue
        # ざっくりテキスト判定（バイナリ可能性を低減）
        p = ROOT / f
        try:
            if p.is_file() and p.stat().st_size <= 512_000:
                with open(p, "rb") as fh:
                    chunk = fh.read(2048)
                    if b"\0" in chunk:
                        continue
                out.append(f)
        except Exception:
            continue
    return out

def ensure_check_index(candidates: List[str]):
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    if not CHECK_INDEX.exists():
        lines = [
            "# Refactor Checklist Index",
            "",
            f"- ルールファイル: {'docs/rule.md (あり)' if (DOCS_DIR/'rule.md').exists() else 'なし'}",
            "- リファクタレベル: 1(不要)〜5(必須)",
            "",
            "## 対象ファイル",
        ]
        lines += [f"- [ ] {p}" for p in candidates]
        CHECK_INDEX.write_text("\n".join(lines), encoding="utf-8")

    # 各ファイルの個別 checklist 雛形
    for path in candidates:
        fpath = (CHECK_DIR / path).with_suffix(Path(path).suffix + ".md")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        if not fpath.exists():
            tpl = [
                f"# Checklist: {path}",
                "",
                "- [ ] 命名の明確化（変数/関数/クラス）",
                "- [ ] 責務の分離・早期 return 等でネスト削減",
                "- [ ] 不要コード/コメントの整理",
                "- [ ] 例外/エラー処理の強化",
                "- [ ] 性能（不要ループ/重複処理の削減）",
                "- [ ] セキュリティ（入力検証/エスケープ等）",
                "- [ ] I/O/副作用の最小化・不変データの活用",
                "- [ ] テスト影響確認・補助テスト案",
                "- [ ] ドキュメンテーション（docstring/JSDoc等）",
                "- [ ] フォーマッタ/リンタ適用",
                "",
                "> 自動更新: LLM リファクタ完了時に本 checklist を ✔ にします。",
            ]
            fpath.write_text("\n".join(tpl), encoding="utf-8")

def parse_unchecked() -> List[str]:
    if not CHECK_INDEX.exists():
        return []
    out = []
    for ln in CHECK_INDEX.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^- \[\s\]\s+(.+)$", ln)
        if m:
            out.append(m.group(1).strip())
    return out

def mark_checked_in_index(target: str, level: int, reason: str):
    if not CHECK_INDEX.exists():
        return
    def _short(s: str, n=140):
        s = s.replace("\n", " ")
        return (s[: n - 1] + "…") if len(s) > n else s
    lines = CHECK_INDEX.read_text(encoding="utf-8").splitlines()
    out = []
    pat = re.compile(rf"^- \[\s\]\s+{re.escape(target)}\s*$")
    done = False
    for ln in lines:
        if pat.match(ln) and not done:
            out.append(f"- [x] {target}  （レベル {level}/5）{_short(reason)}")
            done = True
        else:
            out.append(ln)
    CHECK_INDEX.write_text("\n".join(out), encoding="utf-8")

def mark_all_checked_in_item(target: str):
    fpath = (CHECK_DIR / target).with_suffix(Path(target).suffix + ".md")
    if not fpath.exists():
        return
    lines = fpath.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        out.append(ln.replace("- [ ]", "- [x]"))
    out.append("")
    out.append(f"_Auto-checked by LLM refactor for `{target}`_")
    fpath.write_text("\n".join(out), encoding="utf-8")

def build_client() -> genai.Client:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT が未設定です。")
    return genai.Client(vertexai=True, project=project, location=location)

def call_llm(file_path: str, content: str) -> dict:
    """
    返却: {"level": int, "reason": str, "refactored_code": str, "summary": str}
    JSON 固定出力を要求（response_mime_type=application/json）。
    """
    rules = ""
    rule_file = DOCS_DIR / "rule.md"
    if rule_file.exists():
        try:
            rules = rule_file.read_text(encoding="utf-8")
        except Exception:
            rules = ""

    checklist_path = (CHECK_DIR / file_path).with_suffix(Path(file_path).suffix + ".md")
    checklist = ""
    if checklist_path.exists():
        checklist = checklist_path.read_text(encoding="utf-8")

    system_instruction = (
        "あなたはシニアエンジニアです。安全・最小差分でリファクタし、"
        "意味の変更は避け、可読性/保守性/性能/セキュリティを改善してください。"
        "無理に変更せず、リファクタ不要ならその旨を正直に伝えてください。"
        "出力は必ず JSON のみ。"
    )
    user_prompt = f"""
【前提】
- 対象ファイル: {file_path}
- コーディング規約: {'あり' if rules else 'なし'}
- チェックリスト（要参照）:
{checklist or '(初期雛形)'}

【元コード】
<code>
{content}
</code>

【出力仕様（JSON厳守）】
{{
  "level": 1..5,            // リファクタ必要性の5段階（5=必須）
  "reason": "string",       // レベルの理由（簡潔に）
  "summary": "string",      // 変更要約（箇条書き可）
  "refactored_code": "string" // 完成後の全コード（同言語）
}}
コーディング規約があればそれに従い、無ければ一般的ベストプラクティスで。
"""

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
    client = build_client()
    cfg = genai_types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1,
        response_mime_type="application/json",
    )
    resp = client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=cfg,
    )
    text = getattr(resp, "text", "") or ""
    try:
        data = json.loads(text)
        # 軽いバリデーション
        if "refactored_code" not in data:
            raise ValueError("refactored_code がありません。")
        if "level" not in data or "reason" not in data:
            raise ValueError("level または reason がありません。")
        return data
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM 応答が JSON ではありません: {e}") from e

def refactor_one(file_rel: str):
    target = ROOT / file_rel
    if not target.exists():
        raise RuntimeError(f"ファイルが見つかりません: {file_rel}")

    before = target.read_text(encoding="utf-8", errors="ignore")
    data = call_llm(file_rel, before)
    after = data["refactored_code"]
    level = int(data.get("level", 3))
    reason = str(data.get("reason", "")).strip()
    summary = str(data.get("summary", "")).strip()

    changed = (before != after)
    if changed:
        target.write_text(after, encoding="utf-8")
        mark_all_checked_in_item(file_rel)
        mark_checked_in_index(file_rel, level, reason)

    # PR タイトル/本文/ブランチ名などを作る
    sha = hashlib.sha1((file_rel + reason).encode("utf-8")).hexdigest()[:10]
    branch_suffix = f"{sha}"
    pr_title = f"Refactor: {file_rel} (level {level}/5)"
    body_dir = ROOT / ".github" / "ci" / "_pr_bodies"
    body_dir.mkdir(parents=True, exist_ok=True)
    body_path = body_dir / f"{sha}.md"
    body_md = f"""## LLM リファクタ結果

**対象**: `{file_rel}`  
**リファクタレベル**: **{level}/5**  
**理由**: {reason}

### 要約
{summary or '(概要なし)'}

### チェックリスト
- インデックス: `docs/check.md` の該当行を [x] に更新済み
- 個別項目: `docs/checklists/{file_rel}{Path(file_rel).suffix}.md` も更新済み

> 生成: Vertex AI / Gemini（SDK: google-genai）
"""
    body_path.write_text(body_md, encoding="utf-8")

    commit_message = f"refactor({file_rel}): level {level}/5 - {reason[:60]}"
    # 出力
    set_output("changed", "true" if changed else "false")
    set_output("pr_title", pr_title)
    set_output("pr_body_path", str(body_path))
    set_output("branch_suffix", branch_suffix)
    set_output("commit_message", commit_message)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["prepare", "refactor-single"])
    ap.add_argument("--file")
    ap.add_argument("--target-branch", default="develop")
    ap.add_argument("--batch-size", default="1")
    ap.add_argument("--include-globs", default=",".join(DEFAULT_INCLUDE))
    ap.add_argument("--exclude-globs", default=",".join(DEFAULT_EXCLUDE))
    ap.add_argument("--init-only", default="false")
    ap.add_argument("--dry-run", default="false")
    args = ap.parse_args()

    include_globs = [s.strip() for s in (args.include_globs or "").split(",") if s.strip()]
    exclude_globs = [s.strip() for s in (args.exclude_globs or "").split(",") if s.strip()]

    if args.mode == "prepare":
        candidates = pick_candidates(include_globs, exclude_globs)
        ensure_check_index(candidates)
        unchecked = parse_unchecked()
        n = max(1, int(str(args.batch_size)))
        picked = unchecked[:n] if str(args.init_only).lower() != "true" else []
        set_output("files", json.dumps(picked))  # matrix 用
        return

    if args.mode == "refactor-single":
        if not args.file:
            raise RuntimeError("--file が必要です。")
        refactor_one(args.file)
        return

if __name__ == "__main__":
    main()
