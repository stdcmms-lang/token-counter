---
name: token-film
description: Create a deterministic, shareable animated token film for one local Claude Code session, with browser playback and MP4 export.
user-invocable: true
disable-model-invocation: false
argument-hint: "[session prefix, this session, or longest recent session]"
---

Create a token film with this plugin's deterministic script.

Film the session prefix the user names. If they ask for this session, use
--current. If they request a film without choosing a session, run --list and
choose the listed session with the most nonzero steps; break ties by span,
tokens, then listing order. Rows marked unavailable are not candidates.
Say which prefix and step count you chose.

On Windows:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/film.py" --current --no-open
```

On macOS/Linux:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/film.py" --current --no-open
```

Replace --current with --session PREFIX for a named session. Use --list to
list recent main transcripts. Supported additional options are --sessions-root
PATH, --prices PATH, --out PATH, --json PATH, --no-open, and --version.
Keep the quoted script path exactly as shown.

If --current reports that its session variable is unavailable, report that
limitation; do not silently film a different session.

Read the terminal summary and material quality counters. Link the generated
HTML and any requested JSON. Explain that the film shows only the main
transcript; omitted subagent usage is listed separately.

Call dollar figures API list value, not spend or a bill. Unknown prices and
partial usage are disclosed by the script.

Never inspect or repeat prompt, output, thinking, tool input, or tool-result
content. Never open account or credential files, external tool-result files,
or image URLs. Do not fetch prices or install packages.

Open the HTML in a browser. It starts paused on a poster. Select Play to start
the film with sound, then Save MP4 to download the video. Export support
depends on the browser's local codec capabilities.

Do not share, upload, publish, or invoke token-share as part of making a film.
