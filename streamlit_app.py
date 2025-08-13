
# streamlit_app.py
# Laboratory Inventory Dashboard — Manager / Purchasing / Research views
# Run with: streamlit run streamlit_app.py

import io
import os
import time
import base64
from datetime import datetime, timedelta, date

import pandas as pd
import numpy as np
import streamlit as st

# ------------ Config & Styling ------------
st.set_page_config(page_title="Lab Inventory Dashboard", page_icon="🧪", layout="wide")

# Attempt to show a header logo if present
def header():
    logo_path = "mmcccl_logo.png"
    cols = st.columns([1,6,1])
    with cols[1]:
        st.markdown("<div style='text-align:center'>", unsafe_allow_html=True)
        if os.path.exists(logo_path):
            st.image(logo_path, width=220)
        st.markdown("<h1 style='margin-top:0'>Patho Core Inventory</h1>", unsafe_allow_html=True)
        st.caption("A shared dashboard for Managers, Purchasing, and Researchers")
        st.markdown("</div>", unsafe_allow_html=True)

# Small helpers
@st.cache_data(show_spinner=False)
def load_default_file():
    default_path = "/mnt/data/patho_core_inventory.xlsx"
    if os.path.exists(default_path):
        try:
            return pd.read_excel(default_path)
        except Exception:
            pass
    return None

def normalize_columns(df):
    # Lowercase and strip
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lower_map = {c: str(c).lower() for c in df.columns}

    # Guess columns
    def best_match(candidates):
        # Return first existing column that matches any candidate keyword
        for c in df.columns:
            low = lower_map[c]
            for k in candidates:
                if k in low:
                    return c
        return None

    desc_col = best_match(["description", "item", "name", "product"])
    qty_col  = best_match(["qty", "quantity", "stock", "on hand", "on-hand"])
    date_col = best_match(["delivery", "eta", "arriv", "expected", "date"])

    # Ensure required columns exist
    # If missing, create reasonable defaults
    if desc_col is None:
        desc_col = df.columns[0]
    if qty_col is None:
        # create qty from zeros if absent
        df["Quantity"] = 0
        qty_col = "Quantity"
    if date_col is None:
        df["Delivery Date"] = pd.NaT
        date_col = "Delivery Date"

    # Standardize names
    rename = {desc_col: "Item Description", qty_col: "QTY", date_col: "Delivery Date"}
    df = df.rename(columns=rename)

    # Coerce types
    if "QTY" in df.columns:
        df["QTY"] = pd.to_numeric(df["QTY"], errors="coerce").fillna(0).astype(int)
    if "Delivery Date" in df.columns:
        df["Delivery Date"] = pd.to_datetime(df["Delivery Date"], errors="coerce")

    # Optional helpers if present
    for guess, standard in [
        (["vendor","supplier","manufacturer"], "Vendor"),
        (["catalog","sku","cat #","cat#","id"], "Catalog #"),
        (["location","freezer","room","shelf","box"], "Location"),
        (["project","study","pi","team"], "Project/PI"),
        (["unit","pack size","uom"], "Unit"),
        (["price","cost"], "Unit Cost"),
    ]:
        col = None
        for c in df.columns:
            low = str(c).lower()
            if any(k in low for k in guess):
                col = c; break
        if col and standard not in df.columns:
            df.rename(columns={col: standard}, inplace=True)

    return df

def kpi_card(label, value, help_text=None):
    st.metric(label, value, help=help_text if help_text else None)

def style_inventory(df, low_stock_threshold=5):
    df = df.copy()
    # Build style via pandas Styler
    def highlight_low(val):
        try:
            v = int(val)
            if v <= low_stock_threshold:
                return "background-color:#ffeaea; color:#b00020; font-weight:600"
        except:
            pass
        return ""
    def highlight_soon(s):
        # For Delivery Date column only
        styles = []
        today = pd.Timestamp(date.today())
        soon = today + pd.Timedelta(days=7)
        for v in s:
            if pd.isna(v):
                styles.append("")
            elif v <= soon and v >= today:
                styles.append("background-color:#e8f5ff; color:#003f88; font-weight:600")
            elif v < today:
                styles.append("background-color:#fff5e6; color:#8a4b00;")
            else:
                styles.append("")
        return styles

    styler = df.style
    if "QTY" in df.columns:
        styler = styler.applymap(highlight_low, subset=["QTY"])
    if "Delivery Date" in df.columns:
        styler = styler.apply(highlight_soon, subset=["Delivery Date"])
        styler = styler.format({"Delivery Date": lambda x: "" if pd.isna(x) else x.strftime("%Y-%m-%d")})
    return styler

def save_changes(df, original_path=None):
    # Save a timestamped copy locally and provide a download button
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_xlsx = f"/mnt/data/inventory_updated_{timestamp}.xlsx"
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Inventory")
    return out_xlsx

# ------------- UI -------------
header()

with st.sidebar:
    st.subheader("Load Data")
    uploaded = st.file_uploader("Upload inventory file (Excel/CSV)", type=["xlsx","xls","csv"])
    if uploaded is not None:
        if uploaded.name.endswith(".csv"):
            df_raw = pd.read_csv(uploaded)
        else:
            df_raw = pd.read_excel(uploaded)
    else:
        df_raw = load_default_file()

    if df_raw is None or df_raw.empty:
        st.info("No file uploaded. Using an empty template. Upload a file or place patho_core_inventory.xlsx in /mnt/data.")
        df_raw = pd.DataFrame({"Item Description": [], "QTY": [], "Delivery Date": []})

    # Thresholds
    st.subheader("Display Options")
    low_stock_threshold = st.number_input("Low stock threshold", min_value=0, max_value=9999, value=5, step=1)
    show_only_low = st.checkbox("Show only low stock", value=False)
    show_arriving_soon = st.checkbox("Show only arriving within 7 days", value=False)
    search_text = st.text_input("Search (item/vendor/catalog/location)")

df = normalize_columns(df_raw)

# Enrich with helper columns
today = pd.Timestamp(date.today())
df["Arriving Soon?"] = df["Delivery Date"].between(today, today + pd.Timedelta(days=7), inclusive="both")
df["Overdue?"] = (df["Delivery Date"] < today)

# Filtering
mask = pd.Series(True, index=df.index)
if search_text:
    search_low = search_text.lower()
    search_cols = [c for c in ["Item Description","Vendor","Catalog #","Location","Project/PI"] if c in df.columns]
    if search_cols:
        mask = mask & df[search_cols].astype(str).apply(lambda s: s.str.lower().str.contains(search_low)).any(axis=1)

if show_only_low and "QTY" in df.columns:
    mask = mask & (df["QTY"] <= low_stock_threshold)

if show_arriving_soon and "Delivery Date" in df.columns:
    mask = mask & (df["Arriving Soon?"] == True)

df_view = df[mask].copy()

# KPIs
col1, col2, col3, col4 = st.columns(4)
total_skus = df["Item Description"].nunique() if "Item Description" in df.columns else len(df)
total_units = int(df["QTY"].sum()) if "QTY" in df.columns else 0
low_count = int((df["QTY"] <= low_stock_threshold).sum()) if "QTY" in df.columns else 0
arriving_week = int(df["Arriving Soon?"].sum()) if "Arriving Soon?" in df.columns else 0

with col1: kpi_card("Total SKUs", f"{total_skus:,}")
with col2: kpi_card("Total Units", f"{total_units:,}")
with col3: kpi_card("Low Stock (≤ threshold)", f"{low_count:,}", help_text="Count of items at/below alert level")
with col4: kpi_card("Arriving This Week", f"{arriving_week:,}", help_text="Based on Delivery Date within 7 days")

# Audience tabs
tab_mgr, tab_buy, tab_res = st.tabs(["👤 Manager View", "🧾 Purchasing View", "🧪 Research View"])

with tab_mgr:
    st.subheader("At-a-Glance")
    left, right = st.columns([2,3])
    with left:
        if "QTY" in df.columns:
            low_df = df.sort_values("QTY").head(10)
            st.markdown("**Lowest Stock Items**")
            st.dataframe(low_df[["Item Description","QTY","Delivery Date"]].assign(
                **{"Delivery Date": low_df["Delivery Date"].dt.strftime("%Y-%m-%d") if "Delivery Date" in low_df else None}
            ), use_container_width=True, hide_index=True)
    with right:
        if "Delivery Date" in df.columns:
            upcoming = df[df["Arriving Soon?"]].sort_values("Delivery Date")
            st.markdown("**Arriving in the Next 7 Days**")
            st.dataframe(upcoming[["Item Description","QTY","Delivery Date"]].assign(
                **{"Delivery Date": upcoming["Delivery Date"].dt.strftime("%Y-%m-%d")}
            ), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Full Inventory (styled)")
    st.dataframe(
        style_inventory(df_view[["Item Description","QTY","Delivery Date"] + 
                    [c for c in ["Vendor","Catalog #","Location","Project/PI","Unit","Unit Cost"] if c in df_view.columns]]),
        use_container_width=True
    )

with tab_buy:
    st.subheader("Purchasing Queue")
    if "Vendor" in df.columns:
        vendor_counts = (df_view.groupby("Vendor")
                              .agg(Items=("Item Description","nunique"),
                                   Units=("QTY","sum"))
                              .reset_index()
                         .sort_values("Items", ascending=False))
        c1, c2 = st.columns([1,2])
        with c1:
            st.markdown("**By Vendor**")
            st.dataframe(vendor_counts, use_container_width=True, hide_index=True)
        with c2:
            # Delivery calendar-like table
            st.markdown("**Delivery Schedule (next 30 days)**")
            horizon = today + pd.Timedelta(days=30)
            sched = df[(df["Delivery Date"].notna()) & (df["Delivery Date"] <= horizon)]
            sched = (sched
                     .assign(Delivery=lambda d: d["Delivery Date"].dt.strftime("%Y-%m-%d"))
                     .sort_values("Delivery Date"))
            st.dataframe(sched[["Delivery","Item Description","QTY"] + ([ "Vendor"] if "Vendor" in sched.columns else [])],
                         use_container_width=True, hide_index=True)
    else:
        st.info("Vendor column not found — upload a file with a Vendor column to group by supplier.")

    st.divider()
    st.subheader("Edit & Commit Changes")
    st.caption("You can adjust QTY and Delivery Date, then save a timestamped copy.")
    editable_cols = [c for c in ["Item Description","QTY","Delivery Date","Vendor","Catalog #","Location","Project/PI","Unit","Unit Cost"] if c in df_view.columns]
    edited = st.data_editor(df_view[editable_cols], num_rows="dynamic", use_container_width=True)
    if st.button("💾 Save Updated Inventory"):
        # Ensure types
        if "Delivery Date" in edited.columns:
            edited["Delivery Date"] = pd.to_datetime(edited["Delivery Date"], errors="coerce")
        if "QTY" in edited.columns:
            edited["QTY"] = pd.to_numeric(edited["QTY"], errors="coerce").fillna(0).astype(int)

        out_path = save_changes(edited)
        st.success(f"Saved: {os.path.basename(out_path)}")
        with open(out_path, "rb") as f:
            st.download_button("⬇️ Download updated Excel", data=f.read(), file_name=os.path.basename(out_path), mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab_res:
    st.subheader("Find Your Stuff")
    # A simple, researcher-friendly view
    pick_cols = [c for c in ["Item Description","QTY","Location","Project/PI","Delivery Date","Vendor","Catalog #","Unit"] if c in df_view.columns]
    st.dataframe(
        df_view[pick_cols].assign(**{"Delivery Date": df_view["Delivery Date"].dt.strftime("%Y-%m-%d") if "Delivery Date" in df_view else None}),
        use_container_width=True, hide_index=True
    )
    st.caption("Tip: Use the search box in the left sidebar to filter by item, vendor, project, or location.")

# Footer
st.markdown("""
<hr style="opacity:.2"/>
<small>Last refreshed: {} • Low-stock threshold: {} • Built with Streamlit</small>
""".format(datetime.now().strftime("%Y-%m-%d %H:%M"), low_stock_threshold), unsafe_allow_html=True)
