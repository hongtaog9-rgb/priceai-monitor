
import os
import re
import csv
import json
import ssl
import smtplib
from pathlib import Path
from datetime import datetime, timezone
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

# ================= 配置 =================

URL = "https://priceai.cc/products/chatgpt-plus"
TARGET_PRICE = 10.0

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "price_state.json"
CSV_FILE = BASE_DIR / "price_history.csv"

# ================= 价格提取 =================

def get_price(text):
    match = re.search(r"[¥￥]\s*([\d,]+(?:\.\d+)?)", text or "")
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def get_offers():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="zh-CN")

        try:
            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000
            )
            page.wait_for_timeout(3000)

            offers = []

            for selector in (
                "table tbody tr",
                '[role="row"]',
                "tr"
            ):
                rows = page.locator(selector)

                for i in range(min(rows.count(), 2000)):
                    row = rows.nth(i)
                    raw = " ".join(row.inner_text().split())
                    price = get_price(raw)

                    if price is None:
                        continue

                    cells = [
                        " ".join(x.split())
                        for x in row.locator("th, td").all_inner_texts()
                    ]

                    status = cells[0] if len(cells) > 0 else ""
                    channel = cells[1] if len(cells) > 1 else ""
                    name = cells[2] if len(cells) > 2 else raw

                    offers.append({
                        "price": price,
                        "name": name,
                        "status": status,
                        "channel": channel,
                        "raw": raw
                    })

                if offers:
                    break

            # 页面不是表格结构时，尝试从文本提取
            if not offers:
                for line in page.locator("body").inner_text().splitlines():
                    price = get_price(line)

                    if price is not None:
                        offers.append({
                            "price": price,
                            "name": line.strip(),
                            "status": "",
                            "channel": "",
                            "raw": line
                        })

            # 去重
            unique = {}
            for item in offers:
                key = (
                    item["name"],
                    item["price"],
                    item["status"]
                )
                unique[key] = item

            return list(unique.values())

        finally:
            browser.close()


# ================= 邮件通知 =================

def send_email(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    sender = os.getenv("EMAIL_FROM", user)

    receivers = [
        x.strip()
        for x in os.environ["EMAIL_TO"].split(",")
        if x.strip()
    ]

    if not receivers:
        raise RuntimeError("没有设置收件邮箱")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg.set_content(body)

    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(
            host,
            port,
            context=context,
            timeout=30
        ) as server:
            server.login(user, password)
            server.send_message(msg)

    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)


# ================= 历史记录 =================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def save_history(now, offers):
    exists = CSV_FILE.exists()

    with CSV_FILE.open(
        "a",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        if not exists:
            writer.writerow([
                "检查时间",
                "价格",
                "商品",
                "库存状态",
                "渠道"
            ])

        for item in offers:
            writer.writerow([
                now,
                item["price"],
                item["name"],
                item["status"],
                item["channel"]
            ])


# ================= 主程序 =================

def main():

    now = datetime.now(timezone.utc).isoformat()

    print("开始检查 PriceAI...")
    print("目标价格：¥10.00")

    offers = get_offers()

    if not offers:
        raise RuntimeError(
            "没有读取到报价，请检查网页结构或网络。"
        )

    # 优先比较有货商品
    in_stock = [
        x for x in offers
        if (
            "有货" in x["status"]
            or not x["status"]
        )
        and "缺货" not in x["raw"]
    ]

    lowest = min(
        in_stock or offers,
        key=lambda x: x["price"]
    )

    price = float(lowest["price"])

    print(f"提取报价：{len(offers)} 条")
    print(f"当前最低价：¥{price:.2f}")

    save_history(now, offers)

    state = load_state()

    old_low = state.get("lowest_price")
    target_sent = state.get("target_sent", False)

    reasons = []

    new_low = (
        old_low is not None
        and price < float(old_low)
    )

    reached_target = (
        price <= TARGET_PRICE
        and not target_sent
    )

    if new_low:
        reasons.append(
            f"刷新历史最低价：¥{float(old_low):.2f} → ¥{price:.2f}"
        )

    if reached_target:
        reasons.append(
            f"已经达到目标价：¥{price:.2f}（目标 ¥10.00）"
        )

    email_sent = False

    if reasons:

        subject = (
            f"PriceAI价格提醒：最低价 ¥{price:.2f}"
        )

        body = (
            "PriceAI ChatGPT Plus 价格监控提醒\n\n"
            + "\n".join(reasons)
            + f"\n当前最低价：¥{price:.2f}"
            + f"\n商品：{lowest['name']}"
            + f"\n库存：{lowest['status']}"
            + f"\n检查时间：{now}"
            + f"\n网址：{URL}"
        )

        send_email(subject, body)
        email_sent = True

        print("邮件发送成功！")

    else:
        print("本轮没有触发价格提醒。")

    # 更新历史最低价
    if old_low is None or (new_low and email_sent):
        state["lowest_price"] = price

    # 目标价提醒只发送一次
    if reached_target and email_sent:
        state["target_sent"] = True

    state["last_checked"] = now
    state["last_price"] = price

    save_state(state)

    print("本轮监控完成。")


if __name__ == "__main__":
    main()
