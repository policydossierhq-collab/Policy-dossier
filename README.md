# Policy Dossier

Nigeria-first policy news site. A scheduled job pulls news from RSS feeds, has ChatGPT write a short teaser and explainer, publishes story pages, and posts to X.

## Files
- `index.html` homepage (reads `stories.json`)
- `policy_dossier.py` fetches news, writes `stories.json` and `stories/*.html`, posts to X
- `.github/workflows/update.yml` runs the script every hour
- `requirements.txt` Python packages

## Setup
1. Push these files to a GitHub repo.
2. Settings > Pages: deploy from the `main` branch, root folder.
3. Settings > Secrets and variables > Actions:
   - Secrets: `OPENAI_API_KEY`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`
   - Variables: `SITE_URL` (your live address, no trailing slash), `X_URL` (e.g. https://x.com/yourhandle)
4. In `index.html`, replace `YOUR_HANDLE` (two places) with your X handle.
5. Actions tab > Update Policy Dossier > Run workflow, to test.

The X app must have read and write permission. Change `OPENAI_MODEL` (a variable or in the script) if the default model is retired.
