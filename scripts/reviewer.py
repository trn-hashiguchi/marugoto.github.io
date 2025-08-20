# scripts/reviewer.py
import os, textwrap
from google import genai

# --- Gemini のクライアント初期化（環境変数から鍵を読む） ---
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

PROMPT = textwrap.dedent("""\
あなたはプロのコードレビュアーです。
以下のGitのコミット差分をレビューし、改善点・良い点・懸念点を指摘してください。
出力はSlackで綺麗に表示されるよう **GitHub Flavored Markdown** で、
各セクションは `##` 見出しで記述してください。
""")

with open("diff.patch", "r", encoding="utf-8") as f:
    diff = f.read()

contents = PROMPT + "\n```diff\n" + diff + "\n```\n"

resp = client.models.generate_content(
    model="gemini-2.0-flash-exp",
    contents=contents,
)
text = resp.text or "(no content)"

# Slack長文（約4万字）対策
MAX = 39_000
if len(text) > MAX:
    text = text[:MAX] + "\n\n…(truncated)…"

with open("review.md", "w", encoding="utf-8") as f:
    f.write(text)
