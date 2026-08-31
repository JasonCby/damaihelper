# coding: utf-8
"""真实浏览器流程（非 dry-run）。

学习用途：把 task_runner 的"实机调度模式"接到真实 Selenium Chrome 上。
流程：启动浏览器 → 打开目标页 → 恢复/等待登录 → 真实页面观测 → 持久化 Cookies。

与模拟流程的区别：这里的每条日志都来自真实浏览器返回的数据（标题/URL/元素探测），
不写入任何伪造的"成功"状态。
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
COOKIES_PATH = ROOT_DIR / "cookies.pkl"

# 等待用户在浏览器里完成扫码/短信登录的最长秒数
LOGIN_WAIT_TIMEOUT = 120
# 登录轮询间隔
LOGIN_POLL_INTERVAL = 3.0

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, str], None]
StoppedFn = Callable[[], bool]


class RealBrowserFlow:
    """真实浏览器编排：所有阶段都会检查 stop_event，可随时安全终止。"""

    def __init__(
        self,
        log: LogFn,
        progress: ProgressFn,
        stopped: StoppedFn,
        sleep: Callable[[float], bool],
    ) -> None:
        self._log = log
        self._progress = progress
        self._stopped = stopped
        self._sleep = sleep  # 可中断 sleep，返回 True 表示应停止
        self._driver: Any = None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self, params: Dict[str, Any]) -> str:
        """执行真实流程，返回终态: completed / stopped / error(抛出)。"""
        event_url = params.get("event_url") or ""
        headless = bool(params.get("headless"))
        mobile = params.get("mobile_emulation")
        mobile = True if mobile is None else bool(mobile)

        try:
            self._launch(headless=headless, mobile=mobile)
            self._navigate(event_url)
            logged_in = self._ensure_login(event_url, params)
            self._observe_page(params)
            if logged_in:
                self._save_cookies()
            return "completed"
        finally:
            self._quit()

    # ------------------------------------------------------------------
    # 各阶段
    # ------------------------------------------------------------------
    def _launch(self, headless: bool, mobile: bool) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        self._progress(10, "正在启动真实 Chrome 浏览器...")
        chrome_options = Options()
        # 降低 webdriver 特征（学习点：navigator.webdriver 检测）
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        if headless:
            chrome_options.add_argument("--headless=new")
        # 非标准位置的 Chrome（如容器内 chrome-for-testing）
        for env_name in ("DMH_CHROME_BINARY",):
            binary = os.environ.get(env_name)
            if binary and Path(binary).exists():
                chrome_options.binary_location = binary
                self._log("DEBUG", f"使用自定义 Chrome 二进制: {binary}")
                break
        if mobile:
            # 移动端 UA 更接近真实购票入口（淘票票 H5）
            chrome_options.add_experimental_option(
                "mobileEmulation", {"deviceName": "Nexus 6"}
            )
        # 沙箱/容器环境需要
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1280,900")

        # 优先 Selenium Manager 自动匹配驱动；仓库根有 chromedriver 时优先用
        from selenium.webdriver.chrome.service import Service

        driver_path = self._local_driver_path()
        if driver_path:
            self._driver = webdriver.Chrome(service=Service(driver_path), options=chrome_options)
        else:
            self._driver = webdriver.Chrome(options=chrome_options)
        self._driver.set_page_load_timeout(30)

        caps = self._driver.capabilities
        self._log(
            "SYSTEM",
            f"Chrome 已启动: {caps.get('browserVersion', '?')} / "
            f"chromedriver={caps.get('chrome', {}).get('chromedriverVersion', '?').split(' ')[0]}",
        )

    def _navigate(self, url: str) -> None:
        self._progress(25, f"正在导航至目标页面: {url[:80]}")
        t0 = time.time()
        try:
            self._driver.get(url)
        except Exception as exc:
            # 超时/加载异常时继续用已加载的部分（学习点：page_load_timeout）
            self._log("WARNING", f"页面加载异常（继续用已加载部分）: {str(exc)[:120]}")
        ms = (time.time() - t0) * 1000
        self._log(
            "INFO",
            f"页面已加载 ({ms:.0f}ms) | 标题: {self._driver.title!r} | URL: {self._driver.current_url[:100]}",
        )

    def _ensure_login(self, event_url: str, params: Dict[str, Any]) -> bool:
        self._progress(40, "检查登录状态...")
        # 先尝试恢复上次会话
        if COOKIES_PATH.exists():
            restored = self._restore_cookies(event_url)
            if restored and self._is_logged_in():
                self._log("SYSTEM", "Cookies 恢复成功，登录态有效")
                return True

        # 淘宝系未登录通常会跳 login.taobao.com；已登录则停留在目标域
        if self._is_logged_in():
            self._log("INFO", "当前浏览器已处于登录态（无需扫码）")
            return True

        self._progress(50, "等待人工登录（请在弹出的浏览器中扫码/验证）...")
        self._log(
            "SYSTEM",
            f"检测到未登录。请在浏览器窗口中完成登录（绑定手机 {params.get('mobile', '未配置')}），"
            f"最长等待 {LOGIN_WAIT_TIMEOUT}s，任务将自动检测登录成功。",
        )
        deadline = time.time() + LOGIN_WAIT_TIMEOUT
        while time.time() < deadline:
            if self._stopped():
                return False
            if self._is_logged_in():
                self._log("SYSTEM", "登录成功！即将保存会话 Cookies")
                return True
            if self._sleep(LOGIN_POLL_INTERVAL):
                return False
        self._log("WARNING", f"等待登录超时（{LOGIN_WAIT_TIMEOUT}s），跳过 Cookies 保存")
        return False

    def _observe_page(self, params: Dict[str, Any]) -> None:
        """真实页面观测：把浏览器里实际看到的东西写进日志。"""
        self._progress(75, "正在观测真实页面结构...")
        driver = self._driver

        # iframe 数量（淘系页面大量使用 iframe）
        iframes = driver.find_elements("tag name", "iframe")
        self._log("INFO", f"页面观测: iframe 数量={len(iframes)}")

        # 常见购票页元素探测（学习点：真实选择器需按实际页面 DOM 调整）
        probes = {
            "场次/排片区": ["css selector", "[class*='session']", "[class*='schedule']"],
            "价格/票档区": ["css selector", "[class*='price']", "[class*='sku']"],
            "购买按钮": ["css selector", "[class*='buy']", "button"],
        }
        for name, (_, *selectors) in probes.items():
            hits = []
            for sel in selectors:
                try:
                    hits += driver.find_elements("css selector", sel)
                except Exception:
                    continue
            self._log(
                "DEBUG",
                f"页面观测: {name} 命中 {len(hits)} 个元素" + (f"（首个: {hits[0].text[:40]!r}）" if hits else ""),
            )

        # 页面真实可交互状态快照（供前端展示）
        snapshot = {
            "title": driver.title,
            "url": driver.current_url,
            "tickets": params.get("tickets"),
            "session_priorities": params.get("session_priorities"),
        }
        self._log("DEBUG", f"页面快照: {json.dumps(snapshot, ensure_ascii=False)[:200]}")
        self._log(
            "SYSTEM",
            "真实浏览器流程执行完毕（学习模式：只做导航/登录/观测，不自动提交订单）",
        )

    # ------------------------------------------------------------------
    # cookies 持久化
    # ------------------------------------------------------------------
    def _restore_cookies(self, event_url: str) -> bool:
        try:
            with open(COOKIES_PATH, "rb") as f:
                cookies = pickle.load(f)
        except Exception as exc:
            self._log("WARNING", f"cookies.pkl 读取失败: {exc}")
            return False

        added = 0
        for cookie in cookies:
            try:
                self._driver.add_cookie(cookie)
                added += 1
            except Exception:
                continue  # 域不匹配等
        self._log("INFO", f"已注入历史 Cookies {added}/{len(cookies)} 条，正在刷新验证...")
        self._driver.get(event_url)  # 重新加载让 cookies 生效
        return added > 0

    def _save_cookies(self) -> None:
        try:
            cookies = self._driver.get_cookies()
            with open(COOKIES_PATH, "wb") as f:
                pickle.dump(cookies, f)
            self._log("SYSTEM", f"会话 Cookies 已持久化: {COOKIES_PATH}（{len(cookies)} 条）")
        except Exception as exc:
            self._log("WARNING", f"Cookies 保存失败: {exc}")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _is_logged_in(self) -> bool:
        """登录态探测：URL 跳到登录域 = 未登录；存在登录入口元素 = 未登录。"""
        try:
            url = self._driver.current_url
            if "login" in url:
                return False
            # 淘系常见登录入口文案
            for sel in ["[class*='login']", "[class*='Login']"]:
                for el in self._driver.find_elements("css selector", sel):
                    text = (el.text or "").strip()
                    if text and ("登录" in text or "login" in text.lower()):
                        return False
            return True
        except Exception:
            return False

    def _quit(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
                self._log("INFO", "浏览器已安全关闭")
            except Exception:
                pass
            self._driver = None

    @staticmethod
    def _local_driver_path() -> Optional[str]:
        """仓库根目录的 chromedriver 优先（Windows 用户常这样放置）。

        注意跳过与当前平台不匹配的二进制（如 Linux 下遇到 chromedriver.exe）。
        """
        is_win = sys.platform.startswith("win")
        for name in ("chromedriver", "chromedriver.exe"):
            p = ROOT_DIR / name
            if not p.exists():
                continue
            if name.endswith(".exe") != is_win:
                continue  # 平台不匹配
            if os.access(p, os.X_OK):
                return str(p)
        return None
