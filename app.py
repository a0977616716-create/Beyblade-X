import io
import json
import os
import re
import pandas as pd
import requests
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image

CSV_FILE = "prices.csv"

# ==================== 頁面基本設定 ====================
st.set_page_config(
    page_title="Doorzo 陀螺免費 AI 自動估價器",
    page_icon="🌀",
    layout="wide",
)

st.title("🌀 Doorzo 戰鬥陀螺自動識別與利潤計算器 (Gemini 免費版)")

# ==================== 自動讀取 / 儲存 CSV 資料庫 ====================
default_price_data = {
    "型號": [
        "UX-09",
        "UX-17",
        "UX-20",
        "BX-01",
        "BX-02",
        "BX-10",
        "CX-01",
        "未知/雜項",
    ],
    "當前時價": [450, 480, 520, 250, 300, 400, 500, 50],
    "預計賣價": [600, 650, 700, 350, 420, 550, 680, 100],
}


def load_price_data():
    if os.path.exists(CSV_FILE):
        return pd.read_csv(CSV_FILE)
    else:
        df = pd.DataFrame(default_price_data)
        df.to_csv(CSV_FILE, index=False)
        return df


if "price_df" not in st.session_state:
    st.session_state["price_df"] = load_price_data()

# ==================== 側邊欄：設定區 ====================
with st.sidebar:
    st.header("⚙️ 系統參數設定")

    env_api_key = os.getenv("GEMINI_API_KEY", "")

    if env_api_key:
        api_key = env_api_key
        st.success("🔒 已自動載入後台隱藏的 API Key")
    else:
        api_key = st.text_input(
            "Google Gemini API Key", value="", type="password"
        )

    st.subheader("💰 匯率與費用設定")
    jpy_rate = st.number_input(
        "日幣對台幣匯率", value=0.21, step=0.005, format="%.3f"
    )
    shipping_fee_per_unit = st.number_input(
        "預估每顆國際運費 (TWD)", value=70, step=10
    )
    doorzo_fee = st.number_input("Doorzo 代購手續費 (TWD)", value=50, step=10)

    st.subheader("📦 型號價格資料庫 (TWD)")
    st.caption("在下方修改或新增後，點擊「儲存價格資料庫」即可保存！")

    edited_df = st.data_editor(
        st.session_state["price_df"],
        num_rows="dynamic",
        key="price_db_editor",
    )

    if st.button("💾 儲存價格資料庫", type="primary"):
        edited_df.to_csv(CSV_FILE, index=False)
        st.session_state["price_df"] = edited_df
        st.success("✅ 修改已成功儲存！重新整理也不會變回預設值。")

    price_db = {}
    for idx, row in edited_df.iterrows():
        if pd.notna(row["型號"]):
            price_db[str(row["型號"])] = {
                "current_market_price": float(row["當前時價"])
                if pd.notna(row["當前時價"])
                else 0,
                "selling_price": float(row["預計賣價"])
                if pd.notna(row["預計賣價"])
                else 0,
            }


# ==================== Doorzo API 與 靜態網頁解析雙備援 ====================
def fetch_doorzo_info(doorzo_url):
    jpy_price = 0.0
    img_bytes = None

    match = re.search(r'https?://[^\s]+', doorzo_url)
    clean_url = match.group(0) if match else doorzo_url.strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.doorzo.com/",
    }

    # 嘗試從 URL 提取商品平台與 ID (例如 rakuma/detail/e3b9a56dc6d17c2faec1b95afb6530ed)
    url_parts = clean_url.split("/")
    item_id = ""
    site_type = ""

    for i, part in enumerate(url_parts):
        if part in ["rakuma", "mercari", "paypay", "yahoo"]:
            site_type = part
            if i + 2 < len(url_parts) and url_parts[i + 1] == "detail":
                item_id = url_parts[i + 2].split("?")[0]
            elif i + 1 < len(url_parts):
                item_id = url_parts[i + 1].split("?")[0]

    # 方案 1: 直接請求 Doorzo API
    if item_id:
        try:
            api_url = f"https://www.doorzo.com/api/item/detail?site={site_type}&id={item_id}"
            res = requests.get(api_url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                item_data = data.get("data", {}) or data.get("result", {})
                if item_data:
                    jpy_price = float(
                        item_data.get("price")
                        or item_data.get("jpyPrice")
                        or 0
                    )
                    img_url = item_data.get("cover") or item_data.get(
                        "image"
                    ) or (item_data.get("images", [None])[0])
                    if img_url:
                        img_res = requests.get(img_url, headers=headers, timeout=5)
                        if img_res.status_code == 200:
                            img_bytes = img_res.content
        except Exception:
            pass

    # 方案 2: 若 API 失敗，解析 HTML 原始碼中的嵌入 JSON
    if jpy_price == 0 or not img_bytes:
        try:
            res = requests.get(clean_url, headers=headers, timeout=10)
            if res.status_code == 200:
                html = res.text

                # 尋找 HTML 內的 JSON 數據 (Next.js / Nuxt 頁面狀態)
                json_matches = re.findall(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    html,
                )
                if json_matches:
                    page_data = json.loads(json_matches[0])
                    # 遞迴搜尋 JSON 中的價格與圖片
                    str_data = json.dumps(page_data)

                    prices = re.findall(r'"price":\s*([0-9]+)', str_data)
                    if prices:
                        valid_prices = [
                            float(p) for p in prices if float(p) >= 100
                        ]
                        if valid_prices:
                            jpy_price = valid_prices[0]

                    imgs = re.findall(r'https://[^\s"]+\.(?:jpg|png|jpeg)', str_data)
                    for img_u in imgs:
                        if not any(
                            k in img_u.lower()
                            for k in ["banner", "logo", "icon", "avatar"]
                        ):
                            i_res = requests.get(img_u, headers=headers, timeout=5)
                            if (
                                i_res.status_code == 200
                                and len(i_res.content) > 15000
                            ):
                                img_bytes = i_res.content
                                break

                # 備用純文字正則搜尋
                if jpy_price == 0:
                    matches = re.findall(r'[￥¥]\s*([0-9,]+)', html)
                    if matches:
                        p_vals = [
                            float(m.replace(",", ""))
                            for m in matches
                            if float(m.replace(",", "")) >= 100
                        ]
                        if p_vals:
                            jpy_price = p_vals[0]
        except Exception as e:
            st.error(f"解析發生例外情況：{e}")

    return jpy_price, img_bytes


# ==================== 免費版 Gemini AI 圖像辨識 ====================
def analyze_image_with_gemini(image_bytes, available_models, gemini_api_key):
    client = genai.Client(api_key=gemini_api_key)
    image = Image.open(io.BytesIO(image_bytes))

    prompt = f"""
    請仔細分析這張照片中的 Beyblade (戰鬥陀螺) 或其盒裝/零件。
    參考常見型號清單：{available_models}。

    請幫我辨識出圖片中出現的陀螺型號以及對應的數量。
    如果遇到無法確定但看起來是陀螺零件或商品的，可以歸類為 "未知/雜項"。

    請嚴格只輸出標準 JSON 格式，範例如下：
    {{
        "detected_items": [
            {{"model": "UX-09", "quantity": 1}},
            {{"model": "UX-17", "quantity": 1}}
        ]
    }}
    """

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[image, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )

    result_json = json.loads(response.text)
    return result_json.get("detected_items", [])


# ==================== 主介面：輸入與計算 ====================
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("1. 輸入商品資訊")
    input_type = st.radio("選擇輸入方式：", ["貼上 Doorzo 網址", "直接上傳圖片"])

    if input_type == "貼上 Doorzo 網址":
        url = st.text_input("請貼上 Doorzo 商品頁面連結：")

        if url and url != st.session_state.get("last_url"):
            with st.spinner("自動擷取 Doorzo 商品價格與圖片中..."):
                jpy_price, fetched_img = fetch_doorzo_info(url)
                st.session_state["last_url"] = url

                if jpy_price > 0:
                    st.session_state["jpy_price"] = jpy_price
                    st.success(f"✅ 自動擷取日幣價格：￥{jpy_price:,.0f} JPY")
                else:
                    st.warning("⚠️ 日幣價格抓取失敗，請手動輸入。")

                if fetched_img:
                    st.session_state["image_bytes"] = fetched_img
                    st.success("✅ 自動擷取商品圖片成功！")
                else:
                    st.warning("⚠️ 圖片抓取失敗，請手動補上傳。")

        jpy_price_input = st.number_input(
            "商品日幣售價 (JPY) [可手動調整]：",
            value=float(st.session_state.get("jpy_price", 0)),
            step=100.0,
        )

        uploaded_fallback = st.file_uploader(
            "若無自動顯示圖片，請在此拖拽補傳圖片：",
            type=["jpg", "png", "jpeg"],
        )
        if uploaded_fallback:
            st.session_state["image_bytes"] = uploaded_fallback.read()

    else:
        jpy_price_input = st.number_input(
            "請輸入商品日幣總價 (JPY)：", value=0.0, step=100.0
        )
        uploaded_file = st.file_uploader(
            "上傳商品照片：", type=["jpg", "png", "jpeg"]
        )
        if uploaded_file:
            st.session_state["image_bytes"] = uploaded_file.read()
            st.session_state["jpy_price"] = jpy_price_input

    if "image_bytes" in st.session_state and st.session_state["image_bytes"]:
        st.image(
            st.session_state["image_bytes"],
            caption="準備進行 AI 分析的圖片",
            use_container_width=True,
        )


with col2:
    st.subheader("2. Gemini AI 分析與利潤試算")

    if st.button("🚀 開始免費辨識並計算利潤", type="primary"):
        if not api_key:
            st.error("請先在左側邊欄輸入您的 Google Gemini API Key！")
        elif "image_bytes" not in st.session_state:
            st.error("請提供有效的圖片！")
        else:
            with st.spinner("Gemini AI 正在辨識圖片中的陀螺型號與數量..."):
                try:
                    items = analyze_image_with_gemini(
                        st.session_state["image_bytes"],
                        list(price_db.keys()),
                        api_key,
                    )

                    st.success("辨識完成！")

                    total_quantity = sum(item["quantity"] for item in items)
                    shipping_units = max(total_quantity, 1)

                    total_shipping_fee = shipping_fee_per_unit * shipping_units

                    calc_jpy = (
                        jpy_price_input
                        if jpy_price_input > 0
                        else st.session_state.get("jpy_price", 0)
                    )
                    product_cost_twd = calc_jpy * jpy_rate
                    total_cost_twd = (
                        product_cost_twd + total_shipping_fee + doorzo_fee
                    )

                    total_expected_revenue = 0.0
                    total_market_value = 0.0

                    result_rows = []
                    for item in items:
                        model = item["model"]
                        qty = item["quantity"]
                        p_info = price_db.get(
                            model,
                            {"current_market_price": 0, "selling_price": 0},
                        )

                        m_price = p_info["current_market_price"] * qty
                        s_price = p_info["selling_price"] * qty

                        total_market_value += m_price
                        total_expected_revenue += s_price

                        result_rows.append(
                            {
                                "型號": model,
                                "數量": qty,
                                "預計單價賣價": p_info["selling_price"],
                                "小計賣價": s_price,
                            }
                        )

                    st.write("### 📋 辨識結果明細")
                    st.dataframe(
                        pd.DataFrame(result_rows), use_container_width=True
                    )

                    profit = total_expected_revenue - total_cost_twd
                    margin = (
                        (profit / total_expected_revenue * 100)
                        if total_expected_revenue > 0
                        else 0
                    )

                    st.write("### 📊 成本與獲利估算")
                    m1, m2, m3 = st.columns(3)
                    m1.metric("預估總進貨成本", f"${total_cost_twd:,.0f} TWD")
                    m2.metric(
                        "預估總銷售額", f"${total_expected_revenue:,.0f} TWD"
                    )
                    m3.metric(
                        "預估淨利潤",
                        f"${profit:,.0f} TWD",
                        delta=f"毛利率 {margin:.1f}%",
                    )

                    st.caption(
                        f"※ 成本細節：日幣 ￥{calc_jpy:,.0f} 折合 TWD ${product_cost_twd:.0f} + 運費 ${total_shipping_fee:.0f} "
                        f"({shipping_units} 顆 × ${shipping_fee_per_unit}) + 手續費 ${doorzo_fee}"
                    )

                except Exception as e:
                    st.error(f"辨識過程中發生錯誤：{e}")
