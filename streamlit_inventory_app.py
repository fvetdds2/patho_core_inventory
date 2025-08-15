
from datetime import datetime, date
from io import BytesIO
import os
import json
import math

import pandas as pd
import numpy as np

import streamlit as st

# Optional: OpenCV for QR decoding (works with st.camera_input without extra packages)
try:
    import cv2
    CV2_AVAILABLE = True
except Exception:
    CV2_AVAILABLE = False

APP_TITLE = "MMCCCL Laboratory Inventory Tracker"
DEFAULT_DATA_PATH = "patho_core_inventory.xlsx"  # Put your Excel beside this app or upload it via sidebar
EXPORT_DIR = "."

st.set_page_config(page_title=APP_TITLE, page_icon="🧪", layout="wide")

# -----------------------------
# Utilities
# -----------------------------

def _snake(s: str) -> str:
    return (
        s.strip()
         .replace(" ", "_")
         .replace("-", "_")
         .replace("/", "_")
         .replace(".", "")
         .lower()
    )

def _coerce_date(s):
    return pd.to_datetime(s, errors="coerce").date() if pd.notna(s) and str(s) != "NaT" else None

def _today():
    return date.today()

@st.cache_data(show_spinner=False)
def load_excel(uploaded_file_or_path) -> pd.DataFrame:
    if uploaded_file_or_path is None:
        if os.path.exists(DEFAULT_DATA_PATH):
            df = pd.read_excel(DEFAULT_DATA_PATH, sheet_name=0)
        else:
            st.warning("No default dataset found. Please upload an Excel file to begin.")
            return pd.DataFrame()
    elif isinstance(uploaded_file_or_path, str):
        df = pd.read_excel(uploaded_file_or_path, sheet_name=0)
    else:
        # Streamlit UploadedFile
        df = pd.read_excel(uploaded_file_or_path, sheet_name=0)
    return df

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mapping = {c: _snake(c) for c in df.columns}
    df = df.rename(columns=mapping)

    # Friendly standard column aliases
    aliases = {
        "item_description": ["item", "description", "name", "item_name", "item_desc"],
        "vendor": ["vender", "supplier", "manufacturer"],
        "ref": ["catalog", "cat_no", "cat", "ref_no", "ref#", "ref_number"],
        "lot": ["lot_number", "lot#", "lot_no"],
        "expiration": ["expiry", "expire_date", "exp_date"],
        "unit_id": ["unit", "unit_index"],
        "location": ["shelf", "storage_location"],
        "qty": ["quantity", "count", "units"],
        "order_unit": ["orderunit", "package_size", "unit_size"],
        "price": ["unit_price", "cost"],
        "delivery_date": ["received_date", "arrival_date"],
        "in_service_date": ["service_date", "start_use_date"],
        "removed_date": ["used_date", "dispose_date", "disposed_date"],
        "status": ["state"],
        "minimum_instock_qty": ["minimum_stock_level", "min_stock", "min_instock", "min_qty", "reorder_level"],
    }

    for canonical, alts in aliases.items():
        if canonical not in df.columns:
            for alt in alts:
                if alt in df.columns:
                    df[canonical] = df[alt]
                    break

    # Ensure all expected columns exist
    expected_cols = list(aliases.keys())
    for c in expected_cols:
        if c not in df.columns:
            df[c] = np.nan

    # Date coercion
    for dcol in ["expiration", "delivery_date", "in_service_date", "removed_date"]:
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce")

    # Status defaults
    if "status" in df.columns:
        df["status"] = df["status"].fillna("")

    return df

def compute_status(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()

    today = pd.to_datetime(_today())

    # Fill status if missing
    def _infer(row):
        if pd.notna(row.get("removed_date")):
            return "used/removed"
        if pd.notna(row.get("expiration")) and row["expiration"] < today:
            return "expired"
        if pd.notna(row.get("in_service_date")):
            return "in service"
        return "in storage"

    df["status"] = df.apply(
        lambda r: r["status"] if isinstance(r.get("status"), str) and r["status"].strip() != "" else _infer(r),
        axis=1,
    )
    return df

def kpi_summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_units": 0, "in_storage": 0, "in_service": 0, "expired": 0, "removed": 0}
    return {
        "total_units": len(df),
        "in_storage": (df["status"] == "in storage").sum(),
        "in_service": (df["status"] == "in service").sum(),
        "expired": (df["status"] == "expired").sum(),
        "removed": (df["status"] == "used/removed").sum(),
    }

def group_key_cols(df: pd.DataFrame):
    cols = [c for c in ["item_description","ref","vendor","order_unit"] if c in df.columns]
    return cols if cols else ["item_description"]

def summarize_by_item(df: pd.DataFrame, near_expiry_days: int = 30) -> pd.DataFrame:
    if df.empty:
        return df

    today = pd.to_datetime(_today())

    # next expiry per group (ignoring removed)
    active = df[df["status"] != "used/removed"].copy()
    next_exp = active.groupby(group_key_cols(df))["expiration"].min().reset_index().rename(columns={"expiration":"next_expiration"})
    counts = active.pivot_table(index=group_key_cols(df), columns="status", aggfunc="size", fill_value=0).reset_index()
    for col in ["in storage", "in service", "expired"]:
        if col not in counts.columns:
            counts[col] = 0
    counts = counts.rename(columns={"in storage":"in_storage","in service":"in_service"})

    # minimum_instock_qty per group (use median if partially filled)
    min_map = df.groupby(group_key_cols(df))["minimum_instock_qty"].median().reset_index()

    out = counts.merge(next_exp, on=group_key_cols(df), how="left").merge(min_map, on=group_key_cols(df), how="left")
    out["minimum_instock_qty"] = out["minimum_instock_qty"].fillna(1).astype(int)
    out["current_stock"] = out["in_storage"] + out["in_service"]
    out["low_stock"] = out["current_stock"] < out["minimum_instock_qty"]
    out["days_to_next_exp"] = (out["next_expiration"] - today).dt.days
    out["near_expiry"] = out["days_to_next_exp"].apply(lambda x: (x is not pd.NaT) and (x is not None) and (x >= 0) and (x <= near_expiry_days))
    out["reorder_qty"] = (out["minimum_instock_qty"] - out["current_stock"]).clip(lower=0)
    return out

def excel_download(df_dict: dict, filename: str) -> BytesIO:
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for sheet, frame in df_dict.items():
            frame.to_excel(writer, index=False, sheet_name=sheet[:31] or "Sheet1")
    bio.seek(0)
    return bio

def ensure_logo():
    # If user placed mmcccl_logo.png into working dir, we will use it
    for candidate in ["mmcccl_logo.png", "./mmcccl_logo.png"]:
        if os.path.exists(candidate):
            return candidate
    return None

def decode_qr_from_image(file) -> str:
    if not CV2_AVAILABLE:
        return ""
    try:
        file.seek(0)
        image_bytes = file.read()
        img_array = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        detector = cv2.QRCodeDetector()
        data, points, _ = detector.detectAndDecode(img)
        return data or ""
    except Exception:
        return ""

# -----------------------------
# Sidebar / Data load
# -----------------------------
logo_path = ensure_logo()
if logo_path:
    st.sidebar.image(logo_path, use_column_width=True)
st.sidebar.markdown(f"### {APP_TITLE}")

st.sidebar.write("**Data Source**")
uploaded = st.sidebar.file_uploader("Upload Excel (.xlsx)", type=["xlsx"], help="If empty, the app tries to load the default dataset in this folder.")
data = load_excel(uploaded)
data = normalize_columns(data)
data = compute_status(data)

# Global settings
with st.sidebar.expander("Settings", expanded=False):
    near_expiry_days = st.number_input("Flag items expiring within (days):", min_value=1, max_value=365, value=30, step=1)
    allow_inline_edit = st.checkbox("Allow inline editing in tables", value=False, help="Enable editing in the Inventory table.")
    st.caption("Edits are saved only when you click **Save Workbook**.")

st.sidebar.divider()
role = st.sidebar.selectbox("View for", ["Dashboard","Inventory","Find Items / QR","Restock Queue","Expenses & Reports","Admin"])

# -----------------------------
# Header
# -----------------------------
col1, col2 = st.columns([1,5])
with col1:
    if logo_path:
        st.image(logo_path, width=120)
with col2:
    st.title(APP_TITLE)
    st.caption("For managers (restocking), technicians (finding items), and stakeholders (spending).")

# -----------------------------
# DASHBOARD
# -----------------------------
if role == "Dashboard":
    kpi = kpi_summary(data)
    a,b,c,d,e = st.columns(5)
    a.metric("Total Units", kpi["total_units"])
    b.metric("In Storage", kpi["in_storage"])
    c.metric("In Service", kpi["in_service"])
    d.metric("Expired", kpi["expired"])
    e.metric("Removed", kpi["removed"])

    st.subheader("Stock Health Summary")
    summary = summarize_by_item(data, near_expiry_days=near_expiry_days)

    # Filters
    f1, f2 = st.columns(2)
    with f1:
        show_only_low = st.checkbox("Show only low stock", value=True)
    with f2:
        show_only_near_expiry = st.checkbox("Show only near-expiry", value=False)

    view = summary.copy()
    if show_only_low:
        view = view[view["low_stock"]]
    if show_only_near_expiry:
        view = view[view["near_expiry"]]

    st.dataframe(view.sort_values(["low_stock","near_expiry","days_to_next_exp"], ascending=[False,False,True]), use_container_width=True)

# -----------------------------
# INVENTORY (technicians & admins)
# -----------------------------
if role == "Inventory":
    st.subheader("Inventory Explorer")
    c1, c2, c3 = st.columns(3)
    with c1:
        q = st.text_input("Search keyword (item/ref/vendor/lot/location)", "")
    with c2:
        status_filter = st.multiselect("Status filter", ["in storage","in service","expired","used/removed"], default=["in storage","in service","expired"])
    with c3:
        loc = st.text_input("Location contains", "")

    view = data.copy()
    if q:
        mask = (
            view["item_description"].astype(str).str.contains(q, case=False, na=False) |
            view["ref"].astype(str).str.contains(q, case=False, na=False) |
            view["vendor"].astype(str).str.contains(q, case=False, na=False) |
            view["lot"].astype(str).str.contains(q, case=False, na=False)
        )
        view = view[mask]
    if status_filter:
        view = view[view["status"].isin(status_filter)]
    if loc:
        view = view[view["location"].astype(str).str.contains(loc, case=False, na=False)]

    if allow_inline_edit:
        edited = st.data_editor(
            view,
            use_container_width=True,
            num_rows="dynamic",
            key="inv_editor"
        )
        st.info("Edited rows in this view will not automatically sync to the full dataset until you press **Apply Changes to Dataset** below.")
        if st.button("Apply Changes to Dataset", type="primary"):
            # Merge changes back to 'data' by index if possible; fall back to concat unique rows
            # We'll rely on combination of columns to match: item_description, ref, lot, unit_id (if present)
            key_cols = [c for c in ["item_description","ref","lot","unit_id"] if c in data.columns]
            if not key_cols:
                st.error("Cannot identify unique keys to merge changes. Please disable inline editing and use the forms below.")
            else:
                # Build key to align
                data["_key"] = data[key_cols].astype(str).agg("|".join, axis=1)
                edited["_key"] = edited[key_cols].astype(str).agg("|".join, axis=1)
                # Overwrite matches
                to_update = data["_key"].isin(edited["_key"])
                data.loc[to_update, edited.columns] = data.loc[to_update].drop(columns=edited.columns, errors="ignore").merge(
                    edited, on="_key", how="left"
                )[edited.columns].values
                data.drop(columns=["_key"], inplace=True, errors="ignore")
                st.success("Dataset updated from edited view.")
    else:
        st.dataframe(view, use_container_width=True)

    st.divider()
    st.subheader("Quick Actions")
    with st.form("quick_actions"):
        colA, colB, colC, colD = st.columns(4)
        with colA:
            find_ref = st.text_input("REF (catalog)")
        with colB:
            find_lot = st.text_input("LOT")
        with colC:
            find_unit = st.text_input("Unit ID (optional)")
        with colD:
            action = st.selectbox("Action", ["Mark In Service","Mark Removed/Used","Update Location","Set Min In-Stock"], index=0)

        new_location = st.text_input("New location (for Update Location)", "")
        new_min = st.number_input("New minimum_instock_qty (group-level)", min_value=0, value=0, step=1)
        submitted = st.form_submit_button("Apply")

        if submitted:
            mask = (data["ref"].astype(str) == find_ref.strip()) & (data["lot"].astype(str) == find_lot.strip())
            if find_unit.strip():
                mask = mask & (data["unit_id"].astype(str) == find_unit.strip())

            if mask.sum() == 0:
                st.error("No matching rows found.")
            else:
                if action == "Mark In Service":
                    data.loc[mask, "in_service_date"] = pd.to_datetime(_today())
                    data.loc[mask, "status"] = "in service"
                    st.success(f"Marked {mask.sum()} unit(s) as in service.")
                elif action == "Mark Removed/Used":
                    data.loc[mask, "removed_date"] = pd.to_datetime(_today())
                    data.loc[mask, "status"] = "used/removed"
                    st.success(f"Marked {mask.sum()} unit(s) as removed/used.")
                elif action == "Update Location":
                    if new_location.strip() == "":
                        st.error("Please provide a new location.")
                    else:
                        data.loc[mask, "location"] = new_location.strip()
                        st.success(f"Updated location for {mask.sum()} unit(s).")
                elif action == "Set Min In-Stock":
                    # Set min at the group level (same item/ref/vendor/order_unit)
                    key = group_key_cols(data)
                    grp_vals = data.loc[mask, key].iloc[0].to_dict()
                    grp_mask = np.ones(len(data), dtype=bool)
                    for k, v in grp_vals.items():
                        grp_mask &= (data[k].astype(str) == str(v))
                    data.loc[grp_mask, "minimum_instock_qty"] = int(new_min)
                    st.success(f"Set minimum_instock_qty={int(new_min)} for group.")

    st.divider()
    st.subheader("Add New Delivery (Multiple Units)")
    with st.form("add_units"):
        colA, colB, colC = st.columns(3)
        with colA:
            item_desc = st.text_input("Item Description *")
            ref_in = st.text_input("REF (catalog) *")
            vendor_in = st.text_input("Vendor")
        with colB:
            lot_in = st.text_input("LOT *")
            order_unit_in = st.text_input("Order Unit (e.g., 2 x 1 Liter)")
            location_in = st.text_input("Storage Location *")
        with colC:
            units_n = st.number_input("How many units arrived?", min_value=1, value=1, step=1)
            min_stock_in = st.number_input("minimum_instock_qty (optional)", min_value=0, value=0, step=1)
            price_in = st.number_input("Unit Price (optional)", min_value=0.0, value=0.0, step=0.01)

        exp_date = st.date_input("Expiration date *")
        delivery_date = st.date_input("Delivery (received) date", value=_today())
        submit_add = st.form_submit_button("Add Units")
        if submit_add:
            rows = []
            start_unit_id = int(data["unit_id"].max()) + 1 if "unit_id" in data.columns and pd.api.types.is_numeric_dtype(data["unit_id"]) and pd.notna(data["unit_id"]).any() else 1
            for i in range(int(units_n)):
                rows.append({
                    "item_description": item_desc,
                    "ref": ref_in,
                    "vendor": vendor_in,
                    "lot": lot_in,
                    "order_unit": order_unit_in,
                    "location": location_in,
                    "unit_id": start_unit_id + i,
                    "expiration": pd.to_datetime(exp_date),
                    "delivery_date": pd.to_datetime(delivery_date),
                    "price": price_in if price_in > 0 else np.nan,
                    "minimum_instock_qty": int(min_stock_in) if min_stock_in > 0 else np.nan,
                    "status": "in storage"
                })
            data = pd.concat([data, pd.DataFrame(rows)], ignore_index=True)
            st.success(f"Added {units_n} unit(s) for {item_desc} (REF {ref_in}).")

# -----------------------------
# FIND ITEMS / QR
# -----------------------------
if role == "Find Items / QR":
    st.subheader("Technician Finder")
    c1, c2 = st.columns([2,1])
    with c1:
        key = st.text_input("Search by REF / LOT / Item / Location", "")
    with c2:
        st.write("")
        st.write("")
        st.caption("Use the camera to scan a QR code on the right.")

    matched = pd.DataFrame()
    if key:
        m = (
            data["ref"].astype(str).str.contains(key, case=False, na=False) |
            data["lot"].astype(str).str.contains(key, case=False, na=False) |
            data["item_description"].astype(str).str.contains(key, case=False, na=False) |
            data["location"].astype(str).str.contains(key, case=False, na=False)
        )
        matched = data[m]
    st.dataframe(matched, use_container_width=True, height=280)

    st.divider()
    st.subheader("Scan QR / Barcode")
    if not CV2_AVAILABLE:
        st.info("OpenCV not detected. QR scanning works best when OpenCV is available. You can still use **Camera Input** to capture an image.")
    snap = st.camera_input("Point your phone camera at the label (QR preferred).")

    if snap is not None:
        decoded = decode_qr_from_image(snap) if CV2_AVAILABLE else ""
        if decoded:
            st.success(f"Detected QR payload: `{decoded}`")
            # Try to parse JSON first; else fallback to plain text lookup
            ref_lot = {}
            try:
                payload = json.loads(decoded)
                if isinstance(payload, dict):
                    ref_lot["ref"] = str(payload.get("ref","")).strip()
                    ref_lot["lot"] = str(payload.get("lot","")).strip()
                    ref_lot["unit_id"] = str(payload.get("unit_id","")).strip()
            except Exception:
                # maybe REF|LOT|UNIT or REF only
                parts = [p.strip() for p in decoded.replace(",", "|").split("|")]
                if len(parts) >= 1: ref_lot["ref"] = parts[0]
                if len(parts) >= 2: ref_lot["lot"] = parts[1]
                if len(parts) >= 3: ref_lot["unit_id"] = parts[2]

            mask = np.ones(len(data), dtype=bool)
            if "ref" in ref_lot and ref_lot["ref"]:
                mask &= (data["ref"].astype(str) == ref_lot["ref"])
            if "lot" in ref_lot and ref_lot["lot"]:
                mask &= (data["lot"].astype(str) == ref_lot["lot"])
            if "unit_id" in ref_lot and ref_lot["unit_id"]:
                mask &= (data["unit_id"].astype(str) == ref_lot["unit_id"])

            found = data[mask]
            if len(found) == 0:
                st.warning("No exact match from QR payload. Showing best matches:")
                # fallback: contains search
                cm = (
                    data["ref"].astype(str).str.contains(ref_lot.get("ref",""), case=False, na=False) |
                    data["lot"].astype(str).str.contains(ref_lot.get("lot",""), case=False, na=False)
                )
                st.dataframe(data[cm].head(50), use_container_width=True)
            else:
                st.success(f"Found {len(found)} matching unit(s). See below:")
                st.dataframe(found, use_container_width=True)
        else:
            st.warning("Could not decode a QR from the image. Try again with a clearer shot.")

# -----------------------------
# RESTOCK QUEUE (manager)
# -----------------------------
if role == "Restock Queue":
    st.subheader("Manager: Restocking & Expirations")
    summary = summarize_by_item(data, near_expiry_days=near_expiry_days)
    st.caption("Items are flagged if current stock < minimum_instock_qty. You can export a restock order sheet.")

    st.dataframe(summary, use_container_width=True)

    # Build restock order proposal
    to_order = summary[summary["reorder_qty"] > 0].copy()
    if to_order.empty:
        st.success("No items are currently below the configured minimum_instock_qty.")
    else:
        st.markdown("#### Proposed Restock")
        to_order["proposed_reorder_qty"] = to_order["reorder_qty"]
        # Allow manual adjustment
        edited_order = st.data_editor(
            to_order[ group_key_cols(data) + ["current_stock","minimum_instock_qty","proposed_reorder_qty","next_expiration","days_to_next_exp","near_expiry"] ],
            use_container_width=True,
            num_rows="fixed",
            key="restock_editor"
        )

        # Compute expected spend if price is available (median per group)
        priced = data.copy()
        priced["price"] = pd.to_numeric(priced["price"], errors="coerce")
        price_map = priced.groupby(group_key_cols(data))["price"].median().reset_index().rename(columns={"price":"unit_price"})
        edited_order = edited_order.merge(price_map, on=group_key_cols(data), how="left")
        edited_order["est_cost"] = edited_order["unit_price"] * edited_order["proposed_reorder_qty"]

        st.markdown("##### Estimated Costs")
        cost_cols = group_key_cols(data) + ["proposed_reorder_qty","unit_price","est_cost"]
        st.dataframe(edited_order[cost_cols], use_container_width=True)

        # Export button
        fname = f"restock_order_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        out = excel_download({"Restock": edited_order[cost_cols]}, fname)
        st.download_button("⬇️ Download Restock Order (Excel)", data=out, file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.divider()
    st.markdown("#### Expired & Near-Expiry Units")
    today = pd.to_datetime(_today())
    expired_units = data[(data["status"] == "expired") | ((data["expiration"] <= (today + pd.Timedelta(days=near_expiry_days))) & (data["status"] != "used/removed"))]
    st.dataframe(expired_units.sort_values("expiration"), use_container_width=True, height=280)

# -----------------------------
# EXPENSES & REPORTS (stakeholders)
# -----------------------------
if role == "Expenses & Reports":
    st.subheader("Spending Overview")
    # Spending by vendor & month using price column
    df = data.copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["month"] = pd.to_datetime(df["delivery_date"], errors="coerce").dt.to_period("M").astype(str)
    spend = df.dropna(subset=["price","month"]).groupby(["vendor","month"])["price"].sum().reset_index().rename(columns={"price":"spend_usd"})
    st.markdown("##### Spend by Vendor & Month")
    st.dataframe(spend.sort_values(["month","vendor"]), use_container_width=True, height=300)

    st.markdown("##### Top Vendors (Total Spend)")
    top_vendors = df.dropna(subset=["price"]).groupby("vendor")["price"].sum().reset_index().rename(columns={"price":"total_spend"}).sort_values("total_spend", ascending=False)
    st.dataframe(top_vendors, use_container_width=True)

    st.markdown("##### Items with Prices (for unit cost reference)")
    ref_prices = df.groupby(group_key_cols(df))["price"].median().reset_index().rename(columns={"price":"median_unit_price_usd"})
    st.dataframe(ref_prices.sort_values("median_unit_price_usd", ascending=False), use_container_width=True)

# -----------------------------
# ADMIN (save/export)
# -----------------------------
if role == "Admin":
    st.subheader("Save & Export")
    st.caption("Download the **current working dataset** (including edits from this session).")
    fname = f"inventory_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    out = excel_download({"Inventory": data}, fname)
    st.download_button("⬇️ Download Inventory (Excel)", data=out, file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.divider()
    st.markdown("#### How to Print Labels/QR (Optional)")
    st.write(\"\"\"
    - Create QR content such as JSON: `{\"ref\": \"6769001\", \"lot\": \"154219\", \"unit_id\": \"3\"}`
    - Or pipe-delimited: `6769001|154219|3`
    - Print on labels and stick to each unit. Use the **Find Items / QR** tab to scan.
    \"\"\")

    st.markdown("#### Notes")
    st.write(\"\"\"
    - **minimum_instock_qty** can be set per item group via the *Inventory* quick action.
    - Items are treated at **unit-level** (each row is a unit). Status auto-infers from dates:
        - `removed_date` → **used/removed**
        - `expiration` < today → **expired**
        - `in_service_date` set → **in service**
        - otherwise → **in storage**
    - Manager view proposes reorder amounts: `max(minimum_instock_qty - (in_storage + in_service), 0)`.
    \"\"\")
