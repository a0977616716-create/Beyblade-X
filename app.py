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
from playwright.sync_api import sync_playwright
import os
import subprocess

# 💡 確保 Playwright 在 Streamlit Cloud 啟動時下載 Chromium
try:
    subprocess.run(["playwright", "install", "chromium"], check=True)
except Exception as e:
    st.error(f"Playwright 瀏覽器安裝失敗: {e}")
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
from playwright.sync_api import sync_playwright
from playwright._impl._driver import compute_driver_executable, get_driver_env

# 💡 確保在 Streamlit Cloud 雲端環境自動下載 Chromium
try:
    import subprocess
    subprocess.run(["playwright", "install", "chromium"], check=True)
except Exception:
    pass
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

    # 1. 優先從系統環境變數讀取 API Key (後台設定)
    env_api_key = os.getenv("GEMINI_API_KEY", "")

    if env_api_key:
        api_key = env_api_key
        st.success("🔒 已自動載入後台隱藏的 API Key")
    else:
        # 2. 若後台未設定，才顯示輸入框供手動填寫 (預設留空)
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


# ==================== Playwright 精準抓價與商品圖 ====================
def fetch_doorzo_info(doorzo_url):
    jpy_price = 0.0
    img_bytes = None

    match = re.search(r'https?://[^\s]+', doorzo_url)
    clean_url = match.group(0) if match else doorzo_url.strip()

    # 精準定位「主商品價格區塊」，排除折價（值引き）、運費與優惠字樣
    js_get_price = """
    () => {
        const candidates = [];
        const allNodes = Array.from(document.querySelectorAll('*'));

        for (let el of allNodes) {
            const txt = (el.innerText || '').trim();
            if (txt.includes('值引き') || txt.includes('折扣') || txt.includes('點につき')) continue;

            const match = txt.match(/¥\\s*([0-9,]+)/) || txt.match(/￥\\s*([0-9,]+)/) || txt.match(/([0-9,]+)\\s*円/);
            if (match) {
                const val = parseFloat(match[1].replace(/,/g, ''));
                if (val >= 100) {
                    candidates.push(val);
                }
            }
        }

        if (candidates.length > 0) {
            return Math.max(...candidates);
        }
        return 0;
    }
    """

    js_get_images = """
    () => {
        const validUrls = [];
        const imgs = Array.from(document.querySelectorAll('img'));
        for (let i of imgs) {
            const src = i.src || '';
            const s = src.toLowerCase();
            const width = i.naturalWidth || i.clientWidth || 0;
            const height = i.naturalHeight || i.clientHeight || 0;
            
            if (s.includes('banner') || s.includes('logo') || s.includes('icon') || s.includes('coupon') || s.includes('activity')) continue;
            
            if (width > 200 && height > 200 && (width / height) < 1.8) {
                validUrls.push(src);
            }
        }
        return validUrls;
    }
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )

        page = context.new_page()

        try:
            page.goto(clean_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            for _ in range(10):
                p_val = page.evaluate(js_get_price)
                if p_val > 0:
                    jpy_price = p_val
                    break
                page.wait_for_timeout(300)

            img_urls = page.evaluate(js_get_images)
            for url in img_urls:
                try:
                    res = requests.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=5,
                    )
                    if res.status_code == 200 and len(res.content) > 15000:
                        img_bytes = res.content
                        break
                except Exception:
                    pass

            if not img_bytes:
                img_bytes = page.screenshot(
                    clip={"x": 50, "y": 100, "width": 700, "height": 600}
                )

        except Exception as e:
            st.error(f"網頁載入時發生錯誤：{e}")
        finally:
            browser.close()

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
        model="gemini-1.5-flash",
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

        # 自動偵測網址變動，免按按鈕！
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
