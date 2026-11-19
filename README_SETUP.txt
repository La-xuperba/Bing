BINANCE GEMS — Image-required deployment

FILES
1) bot.py                         -> repository root
2) requirements.txt               -> repository root
3) .github/workflows/binance-gems.yml -> exact path shown

OFFICIAL BINANCE SQUARE SKILL
The workflow automatically downloads the official Binance Square Post Skill
from https://github.com/binance/binance-skills-hub at runtime and places it in
square-skill/. You do NOT need to upload square-skill manually.
The workflow verifies that scripts/post-image.mjs and scripts/lib.mjs exist.
Node.js 20 is installed by the workflow. The current official scripts use Node
built-ins; no npm install is needed.

IMAGE-ONLY POSTING BEHAVIOUR
The bot requires its generated image and the official post-image.mjs script.
If the image, script, or API key is missing, Square posting fails closed; it will
NOT fall back to text-only posting. The scanner can still send Telegram alerts.
The bot generates its own attention-card image for each Square post.

GITHUB SECRETS
Settings -> Secrets and variables -> Actions -> New repository secret:
- TELEGRAM_BOT_TOKEN       (required for Telegram alerts)
- TELEGRAM_CHAT_ID         (required for Telegram alerts)
- BINANCE_SQUARE_OPENAPI_KEY (required for Binance Square image posts)
- COINGECKO_API_KEY        (optional; CoinGecko market-cap data)

The YAML maps TELEGRAM_BOT_TOKEN to the TELEGRAM_TOKEN environment variable
expected by bot.py. Do not create a plaintext secrets file and never commit
API keys to the repository.

RUN
GitHub -> Actions -> BINANCE GEMS Spot Scanner -> Run workflow.
The scheduled workflow runs every 6 hours (UTC), with each bot run lasting up
to 345 minutes. Manual workflow_dispatch is also enabled.

IMPORTANT
- The Square key is a posting key, not a trading key. Keep it private.
- The official Square image script supports up to 4 images per post; this bot
  sends one generated card image.
- Binance Square OpenAPI has a daily post limit. The bot has its own safety cap.
- The bStocks/tokenized-stock base symbols visible in the provided screenshots
  are excluded by an exact blocklist in bot.py.
