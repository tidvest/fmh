#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreeMCHost 自动续期脚本
流程: 登录 -> 进入服务器 -> Manage 标签 -> 记录续期前到期时间
      -> 点击 Renew now -> 记录续期后到期时间 -> 对比判断是否成功
      -> WxPusher 推送结果

环境变量:
  FMC_EMAIL        登录邮箱
  FMC_PASSWORD     登录密码
  FMC_SERVER_IDS   服务器ID列表, 逗号分隔 (从 URL /app/servers/{id} 中获取)
                   留空则脚本会在登录后自动抓取 Home 页面里所有服务器卡片的链接
  WXPUSHER_TOKEN   WxPusher 的 appToken
  WXPUSHER_UID     WxPusher 的 uid (单人) 或 WXPUSHER_TOPIC_ID (群发用 topicIds)
  HEADLESS         "false" 可关闭无头模式方便本地调试, 默认 true
"""

import os
import re
import sys
import time
import json
import asyncio
import requests
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE_URL = "https://new.freemchost.com"
LOGIN_URL = f"{BASE_URL}/login"
HOME_URL = f"{BASE_URL}/app"

EMAIL = os.environ.get("FMC_EMAIL", "")
PASSWORD = os.environ.get("FMC_PASSWORD", "")
SERVER_IDS_RAW = os.environ.get("FMC_SERVER_IDS", "").strip()
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "")
WXPUSHER_TOPIC_ID = os.environ.get("WXPUSHER_TOPIC_ID", "")


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def wxpusher_send(content: str, summary: str = "FreeMCHost续期通知"):
    if not WXPUSHER_TOKEN:
        log("未配置 WXPUSHER_TOKEN, 跳过推送")
        return
    payload = {
        "appToken": WXPUSHER_TOKEN,
        "content": content,
        "summary": summary,
        "contentType": 1,
    }
    if WXPUSHER_UID:
        payload["uids"] = [WXPUSHER_UID]
    if WXPUSHER_TOPIC_ID:
        payload["topicIds"] = [int(WXPUSHER_TOPIC_ID)]
    if not WXPUSHER_UID and not WXPUSHER_TOPIC_ID:
        log("未配置 WXPUSHER_UID / WXPUSHER_TOPIC_ID, 跳过推送")
        return
    try:
        r = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json=payload,
            timeout=15,
        )
        log(f"WxPusher 推送结果: {r.status_code} {r.text}")
    except Exception as e:
        log(f"WxPusher 推送失败: {e}")


async def fill_and_verify(box, value: str, field_name: str) -> bool:
    """
    填入一个输入框, 并校验值是否真的写进去了。
    如果第一次 fill() 之后读出来的值不对(常见原因: 命中了隐藏的蜜罐字段,
    或页面框架在 hydration 完成前重置了受控组件的值),
    就改用模拟真实键盘输入的方式重试一次。
    返回 True 表示最终确认填入成功, False 表示两次都失败。
    """
    await box.click()
    await box.fill(value)
    val = await box.input_value()
    if val == value:
        return True

    log(f"警告: {field_name} 框填入后值不一致(当前='{val}'), 改用键盘模拟输入重试")
    await box.click()
    await box.fill("")
    await box.type(value, delay=50)
    val = await box.input_value()
    if val == value:
        return True

    log(f"错误: {field_name} 框两次填入均未生效(当前='{val}')")
    return False


async def login(page) -> bool:
    log("打开登录页...")
    # 用 networkidle 而不是 domcontentloaded, 尽量等前端框架(若有)完成 hydration,
    # 减少"填入后被框架内部状态覆盖回空值"的竞态概率
    try:
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    except PWTimeout:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    await page.wait_for_selector("#email", timeout=20000)

    # 诊断: 记录页面上 #email / #password 各有多少个、是否可见
    # (常见的反爬手段之一是放一个隐藏的同 id "蜜罐"输入框抓自动化脚本,
    #  如果数量 > 1, 说明很可能命中了这种情况)
    email_count = await page.locator("#email").count()
    pwd_count = await page.locator("#password").count()
    log(f"诊断: 页面上 #email 数量={email_count}, #password 数量={pwd_count}")
    if email_count > 1 or pwd_count > 1:
        log("诊断: 检测到重复 id, 可能存在隐藏蜜罐字段, 将只对可见元素操作")

    email_box = page.locator("#email").first
    pwd_box = page.locator("#password").first

    # 确保拿到的是真正可见可交互的那个元素, 而不是隐藏的蜜罐
    await email_box.wait_for(state="visible", timeout=5000)
    await pwd_box.wait_for(state="visible", timeout=5000)

    email_ok = await fill_and_verify(email_box, EMAIL, "邮箱")
    pwd_ok = await fill_and_verify(pwd_box, PASSWORD, "密码")

    if not email_ok or not pwd_ok:
        try:
            await page.screenshot(path="screenshot_fill_failed.png", full_page=True)
        except Exception:
            pass
        log("登录中止: 邮箱或密码未能成功填入, 不再点击登录按钮")
        return False

    await page.click("button[type=submit]")
    try:
        await page.wait_for_url(f"{BASE_URL}/app**", timeout=45000)
    except PWTimeout:
        # 跳转超时不代表一定失败: 可能只是这次跳转/网络比较慢,
        # 这里做个兜底检查——看当前页面是否其实已经是控制台
        # (用 URL 是否已经变成 /app, 或页面上是否有登录后才会出现的特征元素来判断)
        already_in_app = False
        try:
            if page.url.startswith(f"{BASE_URL}/app"):
                already_in_app = True
            else:
                # 页面上控制台特有的元素, 任一出现就认为已经登录成功
                # 注意: 多个带引擎前缀(text=)的选择器不能直接用逗号拼在一个
                # locator 字符串里当"或"用, 那样会匹配不到任何东西;
                # 必须用 Locator.or_() 把几个独立 locator 合并成"满足任一个即可"
                dashboard_marker = (
                    page.locator("text=Sign out")
                    .or_(page.locator("text=Top up"))
                    .or_(page.locator("a[href*='/app/servers/']"))
                ).first
                await dashboard_marker.wait_for(state="visible", timeout=10000)
                already_in_app = True
        except Exception:
            already_in_app = False

        if not already_in_app:
            log("登录后未跳转到 /app, 可能账号密码错误或触发了验证码")
            try:
                await page.screenshot(path="screenshot_login_failed.png", full_page=True)
            except Exception:
                pass
            return False

        log("登录跳转较慢, 但兜底检测确认已进入控制台")
    log("登录成功, 已进入控制台")
    try:
        await page.screenshot(path="screenshot_after_login.png", full_page=True)
    except Exception:
        pass
    return True


async def get_server_ids_from_home(page) -> list:
    """
    如果没手动配置 FMC_SERVER_IDS, 从首页抓取所有服务器链接。
    注意: 首页卡片可能是同一台服务器被重复渲染(或者你确实开了多台同名 Free(mini)),
    这里统一按 UUID 去重, 最终列表里同一个 UUID 只会出现一次。
    """
    await page.goto(HOME_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # 首页服务器列表可能是懒加载/分页的, 先滚动到底部确保所有卡片都加载出来
    # 做法: 反复滚动到 body 底部, 每次后等一下看链接数量是否还在增长,
    # 连续两次数量不变就认为已经加载完毕(避免无限滚动卷到死循环)
    prev_count = -1
    stable_rounds = 0
    max_rounds = 20
    for _ in range(max_rounds):
        await page.mouse.wheel(0, 2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        cur_hrefs = await page.eval_on_selector_all(
            "a[href*='/app/servers/']",
            "els => els.map(e => e.getAttribute('href'))",
        )
        cur_count = len(cur_hrefs)
        if cur_count == prev_count:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        prev_count = cur_count

    hrefs = await page.eval_on_selector_all(
        "a[href*='/app/servers/']",
        "els => els.map(e => e.getAttribute('href'))",
    )
    seen = set()
    ids = []
    for h in hrefs:
        m = re.search(r"/app/servers/([a-f0-9-]+)", h or "")
        if m:
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid)
                ids.append(sid)

    if len(hrefs) > len(ids):
        log(
            f"首页发现 {len(hrefs)} 个服务器链接, 去重后实际为 {len(ids)} 台不同服务器 "
            f"(说明有重复渲染或你看到的'很多个'其实是同一台)"
        )
    return ids


async def parse_expiry_block(page) -> str:
    """
    抓取 Manage 页面里 'Time until expiry' 倒计时区块的文本,
    例如 "01 D 12 H 09 M 28 S"
    """
    try:
        block = page.locator("text=Time until expiry").locator("..").locator("..")
        text = await block.inner_text(timeout=5000)
        # 清洗空白
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        # 兜底: 直接抓取页面里所有两位数字+字母D/H/M/S 的组合
        try:
            body_text = await page.inner_text("body")
            m = re.search(r"(\d{1,2}\s*D[\s\S]{0,80}?\d{1,2}\s*S)", body_text)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:
            pass
        return ""


async def renew_one_server(context, server_id: str) -> dict:
    """对单个服务器执行续期, 返回结果字典"""
    page = await context.new_page()
    result = {
        "server_id": server_id,
        "success": False,
        "before": "",
        "after": "",
        "note": "",
    }
    try:
        url = f"{BASE_URL}/app/servers/{server_id}"
        log(f"[{server_id}] 打开服务器页面: {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # 点击 Manage 标签
        manage_tab = page.locator("button:has-text('Manage'), [role=tab]:has-text('Manage')").first
        await manage_tab.click(timeout=10000)
        await page.wait_for_timeout(1500)

        before_text = await parse_expiry_block(page)
        result["before"] = before_text
        log(f"[{server_id}] 续期前剩余时间: {before_text}")
        try:
            await page.screenshot(path=f"screenshot_{server_id[:8]}_before.png", full_page=True)
        except Exception:
            pass

        # 点击 Renew now
        renew_btn = page.locator("button:has-text('Renew now')").first
        if await renew_btn.count() == 0:
            result["note"] = "未找到 Renew now 按钮"
            log(f"[{server_id}] 未找到 Renew now 按钮, 可能不需要续期或界面变化")
            try:
                await page.screenshot(path=f"screenshot_{server_id[:8]}_no_btn.png", full_page=True)
            except Exception:
                pass
            await page.close()
            return result

        is_disabled = await renew_btn.is_disabled()
        if is_disabled:
            result["note"] = "Renew now 按钮当前不可点击(可能还没到续期窗口)"
            result["after"] = before_text
            log(f"[{server_id}] Renew now 按钮不可点击")
            await page.close()
            return result

        await renew_btn.click()
        log(f"[{server_id}] 已点击 Renew now, 等待结果...")

        # 等待可能出现的确认弹窗(如果有的话, 这里做个宽松兼容)
        try:
            confirm_btn = page.locator(
                "button:has-text('Confirm'), button:has-text('Yes'), button:has-text('OK')"
            ).first
            await confirm_btn.click(timeout=3000)
            log(f"[{server_id}] 检测到确认弹窗并已点击确认")
        except PWTimeout:
            pass

        # 等待页面响应/刷新, 给够时间让后端处理
        await page.wait_for_timeout(5000)

        after_text = await parse_expiry_block(page)
        result["after"] = after_text
        log(f"[{server_id}] 续期后剩余时间: {after_text}")
        try:
            await page.screenshot(path=f"screenshot_{server_id[:8]}_after.png", full_page=True)
        except Exception:
            pass

        result["success"] = is_renew_success(before_text, after_text)
        if not result["success"]:
            result["note"] = "续期前后时间未发生预期变化, 需要人工确认"

    except Exception as e:
        result["note"] = f"异常: {e}"
        log(f"[{server_id}] 出错: {e}")
        try:
            await page.screenshot(path=f"screenshot_{server_id[:8]}_error.png", full_page=True)
        except Exception:
            pass
    finally:
        await page.close()
    return result


def parse_duration_to_seconds(text: str) -> int:
    """将 '01 D 12 H 09 M 28 S' 形式的字符串转换为秒数, 解析失败返回 -1"""
    if not text:
        return -1
    d = re.search(r"(\d+)\s*D", text)
    h = re.search(r"(\d+)\s*H", text)
    m = re.search(r"(\d+)\s*M", text)
    s = re.search(r"(\d+)\s*S", text)
    if not (d or h or m or s):
        return -1
    total = 0
    if d:
        total += int(d.group(1)) * 86400
    if h:
        total += int(h.group(1)) * 3600
    if m:
        total += int(m.group(1)) * 60
    if s:
        total += int(s.group(1))
    return total


def is_renew_success(before_text: str, after_text: str) -> bool:
    """
    判断续期是否成功:
    续期后的剩余秒数只要比续期前明显增加, 就认为续期成功。
    注意: 不能用"必须多出至少1小时"这种绝对阈值——
    如果服务器续期前已经很接近单次续期周期的上限(比如这次续期前就有
    23H53M, 上限是23H59M52S), 续期后即使生效, 增量也可能只有几分钟,
    远不到1小时, 之前用 >3600 的写法会把这种"小增量但确实续期成功"的
    情况误判为失败。
    真正能区分"成功"和"没生效"的标准其实很简单: 正常倒计时只会随时间
    自然减少, 所以只要续期后比续期前还要多, 就说明点击生效了; 这里留
    一点小余量(60秒)防止页面读数的轻微抖动造成误判。
    """
    before_sec = parse_duration_to_seconds(before_text)
    after_sec = parse_duration_to_seconds(after_text)
    if before_sec < 0 or after_sec < 0:
        return False
    return (after_sec - before_sec) > 60


async def main():
    if not EMAIL or not PASSWORD:
        log("缺少 FMC_EMAIL / FMC_PASSWORD 环境变量, 退出")
        sys.exit(1)

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        ok = await login(page)
        if not ok:
            wxpusher_send("FreeMCHost 登录失败,请检查账号密码或是否被风控/验证码拦截。")
            await browser.close()
            sys.exit(1)

        if SERVER_IDS_RAW:
            server_ids = [s.strip() for s in SERVER_IDS_RAW.split(",") if s.strip()]
        else:
            server_ids = await get_server_ids_from_home(page)
            log(f"自动抓取到 {len(server_ids)} 个服务器: {server_ids}")

        await page.close()

        for sid in server_ids:
            r = await renew_one_server(context, sid)
            results.append(r)
            await asyncio.sleep(2)

        await browser.close()

    # 汇总消息
    lines = ["FreeMCHost 续期结果汇总:", ""]
    success_count = 0
    for r in results:
        status = "✅ 成功" if r["success"] else "❌ 失败/待确认"
        if r["success"]:
            success_count += 1
        lines.append(
            f"服务器 {r['server_id'][:8]}...\n"
            f"  状态: {status}\n"
            f"  续期前: {r['before'] or '未获取'}\n"
            f"  续期后: {r['after'] or '未获取'}\n"
            f"  备注: {r['note'] or '-'}"
        )
        lines.append("")
    lines.append(f"共 {len(results)} 台, 成功 {success_count} 台")

    summary_text = "\n".join(lines)
    log(summary_text)
    wxpusher_send(summary_text, summary=f"FreeMCHost续期 {success_count}/{len(results)} 成功")

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
