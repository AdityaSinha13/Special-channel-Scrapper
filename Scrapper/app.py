"""
Signify Scraper Suite — one Streamlit front end for the Amazon, Flipkart,
Blinkit, and Swiggy Instamart product scrapers.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Each platform module under scrapers/ keeps the original, already-debugged
extraction logic (JSON-LD / __NEXT_DATA__ / __PRELOADED_STATE__ fallback
chains etc.) — this file is purely the UI + orchestration layer.
"""
import html
import time
import pandas as pd
import streamlit as st

from scrapers import common, amazon, flipkart, blinkit, swiggy_instamart

st.set_page_config(page_title="Signify Scraper Suite", page_icon="🛰️", layout="wide")

# ═══════════════════════════════════════════════════════════════════════
#  PLATFORM REGISTRY — accent colors are each platform's own brand color,
#  so the UI reads as "four live feeds" rather than one generic theme.
# ═══════════════════════════════════════════════════════════════════════
PLATFORMS = {
    "Amazon.in": {
        "module": amazon, "icon": "📦", "id_label": "ASIN", "needs_location": False,
        "accent": "#FF9A00", "accent_dark_text": False,
        "tagline": "requests-based fast path · Selenium only spins up for live sellers",
    },
    "Flipkart": {
        "module": flipkart, "icon": "🛍️", "id_label": "FSN", "needs_location": True,
        "manual_login": True, "accent": "#2874F0", "accent_dark_text": False,
        "tagline": "JSON-LD + __INITIAL_STATE__ · needs a visible, logged-in browser",
    },
    "Blinkit": {
        "module": blinkit, "icon": "🟡", "id_label": "PID", "needs_location": True,
        "accent": "#F8CB46", "accent_dark_text": True,
        "tagline": "4-layer fallback · PRELOADED_STATE → JSON-LD → meta → DOM",
    },
    "Swiggy Instamart": {
        "module": swiggy_instamart, "icon": "🟠", "id_label": "PID", "needs_location": True,
        "accent": "#FC8019", "accent_dark_text": False,
        "tagline": "4-layer fallback · __NEXT_DATA__ → JSON-LD → meta → DOM",
    },
}

# ═══════════════════════════════════════════════════════════════════════
#  THEME — dispatch-board look: dark instrument panel, monospace for
#  anything that's literally a tracking code (PID/ASIN/FSN), each
#  platform keeps its own real brand color as its "channel" signal.
# ═══════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }

:root {
  --bg: #0F1216;
  --panel: #161B22;
  --panel2: #1B212A;
  --border: #262D38;
  --text: #E9ECF1;
  --muted: #838C9C;
  --success: #35D07F;
  --fail: #FF5C6C;
  --warn: #FFB020;
}

[data-testid="stAppViewContainer"] { background: var(--bg); }
[data-testid="stHeader"] { background: rgba(0,0,0,0); }
[data-testid="stSidebar"] { background: var(--panel); border-right: 1px solid var(--border); }
[data-testid="stSidebar"] * { color: var(--text); }
.block-container { padding-top: 1.6rem; }

h1, h2, h3 { font-family: 'IBM Plex Mono', monospace; letter-spacing: -0.01em; }

.eyebrow {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--muted); margin: 2px 0 8px 0; font-weight: 600;
}

/* panel containers (targeted via st.container(key=...) -> .st-key-<name>) */
.st-key-input_panel, .st-key-fields_panel, .st-key-run_panel {
  background: var(--panel2); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px 14px 14px; margin-bottom: 14px;
}
.st-key-hero { border-radius: 12px; padding: 18px 22px; margin-bottom: 18px; }
.st-key-console {
  background: #0A0D11; border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 14px; font-family: 'IBM Plex Mono', monospace;
}

/* id chips */
.chip {
  display: inline-block; font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  background: #11151B; border: 1px solid var(--border); color: var(--muted);
  border-radius: 5px; padding: 3px 8px; margin: 2px 4px 2px 0;
}

/* status badges */
.badge { font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600;
  padding: 2px 8px; border-radius: 4px; letter-spacing: 0.04em; }
.badge-ok   { background: rgba(53,208,127,0.15); color: var(--success); border: 1px solid rgba(53,208,127,0.4); }
.badge-fail { background: rgba(255,92,108,0.15); color: var(--fail); border: 1px solid rgba(255,92,108,0.4); }
.badge-wait { background: rgba(255,176,32,0.15); color: var(--warn); border: 1px solid rgba(255,176,32,0.4); }

/* metrics */
[data-testid="stMetric"] {
  background: var(--panel2); border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 14px;
}
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace; }

/* buttons */
.stButton>button {
  border-radius: 8px; font-weight: 600; border: 1px solid var(--border);
}
.stButton>button[kind="primary"] {
  background: #2B79FF; border-color: #2B79FF;
}

/* dataframe */
[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }

hr { border-color: var(--border) !important; }
</style>
""", unsafe_allow_html=True)


def badge(text: str, kind: str) -> str:
    cls = {"ok": "badge-ok", "fail": "badge-fail", "wait": "badge-wait"}.get(kind, "badge-wait")
    return f'<span class="badge {cls}">{html.escape(str(text))}</span>'


def status_kind(value: str) -> str:
    v = str(value).upper()
    if "SUCCESS" in v or v == "OK":
        return "ok"
    if any(x in v for x in ("FAIL", "BLOCK", "NOT FOUND", "ERROR")):
        return "fail"
    return "wait"


# ── session state defaults ───────────────────────────────────────────────
for key, default in [
    ("results", None), ("results_platform", None), ("running", False),
    ("flipkart_driver", None), ("live_rows", []), ("platform_name", "Amazon.in"),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ═══════════════════════════════════════════════════════════════════════
#  SIDEBAR — control panel: channel picker + input + fields + run config
# ═══════════════════════════════════════════════════════════════════════
st.sidebar.markdown('<div class="eyebrow">Signify Scraper Suite</div>', unsafe_allow_html=True)
st.sidebar.markdown("### 🛰️ Live feed channels")

for name, meta in PLATFORMS.items():
    selected = st.session_state.platform_name == name
    tile_key = f"tile_{name.replace(' ', '_').replace('.', '')}"
    with st.sidebar.container(key=tile_key):
        st.markdown(f"""
        <style>
        .st-key-{tile_key} .stButton>button[kind="primary"] {{
            background: {meta['accent']}; border-color: {meta['accent']};
            color: {'#14171C' if meta['accent_dark_text'] else '#0A0D11'};
        }}
        </style>
        """, unsafe_allow_html=True)
        if st.button(
            f"{meta['icon']}  {name}",
            key=f"btn_{name}",
            type="primary" if selected else "secondary",
            use_container_width=True,
        ):
            st.session_state.platform_name = name
            st.session_state.results = None
            st.rerun()

platform_name = st.session_state.platform_name
plat = PLATFORMS[platform_name]
mod = plat["module"]
id_label = plat["id_label"]
accent = plat["accent"]

st.sidebar.markdown("---")

with st.sidebar.container(key="input_panel"):
    st.markdown(f'<div class="eyebrow">① Input {id_label}s</div>', unsafe_allow_html=True)
    input_mode = st.radio("Source", ["Upload file", "Paste list"], horizontal=True, label_visibility="collapsed")

    raw_ids = []
    if input_mode == "Upload file":
        up = st.file_uploader(f"CSV / Excel / TXT with a {id_label} column", type=["csv", "xlsx", "xls", "txt"])
        if up is not None:
            try:
                raw_ids = common.read_ids_file(up)
            except Exception as e:
                st.error(f"Couldn't read file: {e}")
    else:
        pasted = st.text_area(f"One {id_label} or URL per line", height=120)
        raw_ids = common.parse_pasted_ids(pasted)

    if raw_ids:
        st.markdown(f'<span class="badge badge-ok">{len(raw_ids)} {id_label}s loaded</span>', unsafe_allow_html=True)

with st.sidebar.container(key="fields_panel"):
    st.markdown('<div class="eyebrow">② Fields to scrape</div>', unsafe_allow_html=True)
    selected_fields = {}
    field_items = list(mod.DEFAULT_FIELDS.items())
    fcol1, fcol2 = st.columns(2)
    for i, (field, default_on) in enumerate(field_items):
        col = fcol1 if i % 2 == 0 else fcol2
        with col:
            selected_fields[field] = st.checkbox(field, value=default_on, key=f"{platform_name}_{field}")

with st.sidebar.container(key="run_panel"):
    st.markdown('<div class="eyebrow">③ Run settings</div>', unsafe_allow_html=True)

    headless = True
    if plat.get("manual_login"):
        st.warning("Flipkart needs a visible browser window to log in and set your pincode by hand — this only works when running the app locally on your own machine, not on the deployed server.")
    else:
        headless = st.checkbox(
            "Headless (background) browser", value=True,
            help="Leave this ON when running on a server (no display available). Only turn it off when running locally and a site is blocking headless Chrome.",
        )

    location = ""
    if plat.get("needs_location") and not plat.get("manual_login"):
        location = st.text_input("Pincode / area", value="122017")

    delay_sec = st.slider("Delay per item (seconds)", 1.0, 10.0, 3.5, 0.5)
    max_items = st.number_input(
        "Max items this run (0 = all)", min_value=0, value=0, step=10,
        help="Handy for a quick test batch before committing to a full list.",
    )

# ═══════════════════════════════════════════════════════════════════════
#  MAIN — hero strip, stats, run controls, live console, results
# ═══════════════════════════════════════════════════════════════════════
with st.container(key="hero"):
    st.markdown(f"""
    <div style="background:linear-gradient(135deg, {accent}22, transparent);
                border:1px solid {accent}55; border-left:5px solid {accent};
                border-radius:12px; padding:18px 22px;">
      <div class="eyebrow" style="color:{accent};">CHANNEL ACTIVE</div>
      <div style="font-family:'IBM Plex Mono',monospace; font-size:26px; font-weight:700; color:var(--text);">
        {plat['icon']}  {platform_name}
      </div>
      <div style="color:var(--muted); font-size:13px; margin-top:4px;">{plat['tagline']}</div>
    </div>
    """, unsafe_allow_html=True)

ids_to_run = raw_ids[: max_items] if max_items else raw_ids
active_fields = [f for f, on in selected_fields.items() if on]

s1, s2, s3 = st.columns(3)
s1.metric(f"{id_label}s queued", len(ids_to_run))
s2.metric("Fields selected", len(active_fields))
s3.metric("Est. time", f"~{int(len(ids_to_run) * delay_sec // 60)} min" if ids_to_run else "—")

if ids_to_run:
    with st.expander(f"Preview queued {id_label}s"):
        chips = "".join(f'<span class="chip">{html.escape(str(i))}</span>' for i in ids_to_run[:40])
        st.markdown(chips + (f'<span class="chip">+{len(ids_to_run)-40} more</span>' if len(ids_to_run) > 40 else ""), unsafe_allow_html=True)
else:
    st.info(f"Load {id_label}s in the sidebar to begin.")

st.markdown("---")

# ── Flipkart's two-phase manual-login flow ───────────────────────────────
if plat.get("manual_login"):
    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("① Open Flipkart & log in", disabled=st.session_state.flipkart_driver is not None, use_container_width=True):
            with st.spinner("Launching Chrome…"):
                st.session_state.flipkart_driver = flipkart.open_driver(headless=False)
            st.rerun()
    with b2:
        driver_ready = st.session_state.flipkart_driver is not None
        start_disabled = (not driver_ready) or (not ids_to_run) or (not active_fields) or st.session_state.running
        if st.button("② Start scraping", disabled=start_disabled, type="primary", use_container_width=True):
            st.session_state.running = True
    with b3:
        if st.button("Close browser", disabled=st.session_state.flipkart_driver is None, use_container_width=True):
            try:
                st.session_state.flipkart_driver.quit()
            except Exception:
                pass
            st.session_state.flipkart_driver = None
            st.rerun()

    if st.session_state.flipkart_driver is not None and not st.session_state.running:
        st.success("Browser is open. Log in to Flipkart, set your delivery pincode, then click **② Start scraping**.")

else:
    start_disabled = (not ids_to_run) or (not active_fields) or st.session_state.running
    if st.button(f"▶  Start scraping {platform_name}", disabled=start_disabled, type="primary", use_container_width=True):
        st.session_state.running = True

# ── run the scrape (blocking, with live progress console) ────────────────
if st.session_state.running:
    progress_bar = st.progress(0.0)
    with st.container(key="console"):
        st.markdown('<div class="eyebrow">LIVE FEED</div>', unsafe_allow_html=True)
        status_line = st.empty()
        live_table = st.empty()
    st.session_state.live_rows = []

    def progress_cb(idx, total, row):
        progress_bar.progress(idx / total)
        item_id = row.get(id_label, "")
        item_status = row.get("Scraping Status") or row.get("Status") or ""
        status_line.markdown(
            f'<span class="chip">{idx}/{total}</span> '
            f'<span class="chip">{html.escape(str(item_id))}</span> '
            f'{badge(item_status, status_kind(item_status))}',
            unsafe_allow_html=True,
        )
        st.session_state.live_rows.append(row)
        if len(st.session_state.live_rows) % 3 == 0 or idx == total:
            live_table.dataframe(pd.DataFrame(st.session_state.live_rows[-15:]), use_container_width=True)

    with st.spinner(f"Scraping {len(ids_to_run)} {id_label}s…"):
        try:
            if platform_name == "Amazon.in":
                results = mod.scrape(
                    ids_to_run,
                    {"fields": selected_fields, "max_retries": 3},
                    progress_cb=progress_cb,
                )
            elif platform_name == "Flipkart":
                results = mod.scrape(
                    st.session_state.flipkart_driver, ids_to_run, active_fields,
                    progress_cb=progress_cb,
                )
            elif platform_name == "Blinkit":
                results = mod.scrape(
                    ids_to_run,
                    {"pincode": location, "headless": headless, "delay_sec": delay_sec},
                    progress_cb=progress_cb,
                )
            else:  # Swiggy Instamart
                results = mod.scrape(
                    ids_to_run,
                    {"location": location, "headless": headless, "delay_sec": delay_sec},
                    progress_cb=progress_cb,
                )
            st.session_state.results = results
            st.session_state.results_platform = platform_name
        except Exception as e:
            st.error(f"Scraping stopped early due to an error: {e}")
            st.session_state.results = st.session_state.live_rows
            st.session_state.results_platform = platform_name
        finally:
            st.session_state.running = False

    st.rerun()

# ── results + download ───────────────────────────────────────────────────
if st.session_state.results is not None and st.session_state.results_platform == platform_name:
    df = pd.DataFrame(st.session_state.results)
    ordered = [c for c in mod.ALL_COLS if c in df.columns] + [c for c in df.columns if c not in mod.ALL_COLS]
    df = df[ordered]

    st.markdown("---")
    st.markdown(f'<div class="eyebrow" style="color:{accent};">RESULTS</div>', unsafe_allow_html=True)

    status_col = "Scraping Status" if "Scraping Status" in df.columns else ("Status" if "Status" in df.columns else None)
    if status_col:
        ok_values = {"Success", "SUCCESS"}
        succ = df[status_col].isin(ok_values).sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", len(df))
        c2.metric("Success", int(succ))
        c3.metric("Failed", len(df) - int(succ))

        def _highlight_status(v):
            k = status_kind(v)
            color = {"ok": "#35D07F", "fail": "#FF5C6C", "wait": "#FFB020"}[k]
            return f"color:{color}; font-weight:600;"

        styler = df.style.applymap(_highlight_status, subset=[status_col])
        st.dataframe(styler, use_container_width=True)
    else:
        st.dataframe(df, use_container_width=True)

    header_colors = {"Amazon.in": "FF9A00", "Flipkart": "2874F0", "Blinkit": "F8CB46", "Swiggy Instamart": "FC8019"}
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "⬇ Download Excel",
            data=common.to_excel_bytes(df, sheet_name=platform_name[:30], header_color=header_colors.get(platform_name)),
            file_name=f"{platform_name.replace(' ', '_').replace('.', '')}_output_{int(time.time())}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with dl2:
        st.download_button(
            "⬇ Download CSV",
            data=common.to_csv_bytes(df),
            file_name=f"{platform_name.replace(' ', '_').replace('.', '')}_output_{int(time.time())}.csv",
            mime="text/csv",
            use_container_width=True,
        )
