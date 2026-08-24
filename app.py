import os
import re
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright

# 💡 自動在 Streamlit Cloud 伺服器下載 Playwright 所需的 Chromium 瀏覽器
os.system("playwright install chromium")

# ==================== Playwright 精準抓價與商品圖 ====================
def fetch_doorzo_info(doorzo_url):
    jpy_price = 0.0
    img_bytes = None

    match = re.search(r"https?://[^\s]+", doorzo_url)
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

    try:
        with sync_playwright() as p:
            # 加入 Streamlit Cloud / Linux 容器必備的無頭啟動參數
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )

            page = context.new_page()

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

            browser.close()

    except Exception as e:
        st.error(f"⚠️ 網頁抓取出現異常：{e}")

    return jpy_price, img_bytes
