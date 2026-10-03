# Policy Dossier bot

Every 3 hours this project pulls headlines (Nigeria first, then global), writes a short summary of each with Claude, rebuilds the website in `docs/`, and posts new stories to your X page.

## Setup (about 20 minutes)

1. **GitHub**: create a new repository and upload this whole folder (keep the `.github` folder).
2. **Website**: in the repo go to Settings > Pages, choose "Deploy from a branch", branch `main`, folder `/docs`. Your site appears at the address GitHub shows.
3. **Anthropic API key**: create one at console.anthropic.com. Add it in Settings > Secrets and variables > Actions as `ANTHROPIC_API_KEY`.
4. **X developer keys**: sign in to developer.x.com with the Policy Dossier account, create a project and app, and set the app permissions to **Read and write**. Generate the API key and secret, then the access token and secret (regenerate the token after changing permissions). Add them as secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.
5. **Test first**: go to the Actions tab and run "Update Policy Dossier". With no `AUTO_POST` variable it only updates the site and does not tweet. Check the stories read well.
6. **Go live**: in Settings > Secrets and variables > Actions > Variables, add `AUTO_POST` with the value `true`. Stories are tweeted from then on, at most 3 per run. Note that stories collected while it was off are still unposted, so the first live runs will tweet the newest of them.

## Things to know

- **Check the feed links** at the top of `main.py`. Outlets change their RSS addresses. Open each in a browser; if one fails, replace it. Add or remove outlets freely.
- **Summaries are automatic.** Claude only sees the headline and the short feed snippet, and is told not to add facts. It can still get things wrong. For a news page, skim the site regularly, and consider keeping `AUTO_POST` off until you trust it.
- **Pictures:** each story shows the picture from the outlet's feed (or its social preview image), credited "Image: [outlet]". The image is loaded from the outlet's own site, not copied, and hidden if it fails to load. Publishers own those pictures, so if you want to reuse them outside the page or avoid any risk, ask for permission or use licensed images. The X post also shows the article's picture automatically through the link preview.
- **Sources are linked** on every story, with the outlet's name. Don't paste full articles into the site, that can breach the outlets' copyright.
- **X limits:** free and paid X API plans cap how many posts you can make. Check your plan's current limits and lower `MAX_TWEETS` in `main.py` if needed.
- Change the schedule in `.github/workflows/update.yml` (the `cron` line).
- Run locally with `pip install -r requirements.txt` then `python main.py`.
