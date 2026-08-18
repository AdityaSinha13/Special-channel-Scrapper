# Deploying the Signify Scraper Suite to Render

## What works on the server
- Amazon (headless, always)
- Blinkit (headless, on by default)
- Swiggy Instamart (headless, on by default)

## What does NOT work on the server
- Flipkart — needs a visible browser for manual login + pincode setup.
  Run Flipkart scraping locally on your own machine instead
  (`streamlit run app.py`), not on the deployed version.

## Steps
1. Push this folder to a GitHub repo (must include Dockerfile, app.py,
   scrapers/, requirements.txt).
2. Go to https://render.com -> New -> Web Service.
3. Connect your GitHub repo.
4. Environment: Docker (Render auto-detects the Dockerfile).
5. Instance type: Free tier is fine for light/testing use; upgrade if you
   need it always-on without spin-down delays.
6. Deploy. Render assigns a public URL like https://your-app.onrender.com

## Notes
- Free tier services spin down after inactivity — first request after
  idle can take ~30-60s to wake up. Fine for internal tool use.
- If you need this private (internal pricing data), consider Render's
  paid tier with access restrictions, or put it behind your company VPN,
  rather than leaving the free public URL open.
