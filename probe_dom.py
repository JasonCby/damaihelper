# coding: utf-8
"""多影院尝试 + 滚动触发懒加载 + 长等待，dump 场次结构。"""
import sys
import time

sys.path.insert(0, "/workspace")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-blink-features=AutomationControlled")
opts.add_argument("--window-size=1280,2400")
opts.binary_location = "/tmp/cft/chrome-linux64/chrome"
driver = webdriver.Chrome(service=Service("/tmp/cft/chromedriver-linux64/chromedriver"), options=opts)
driver.set_page_load_timeout(40)

JS_FIND = """
const timeEls = [];
const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
while (walker.nextNode()) {
  if (/^\\d{1,2}:\\d{2}$/.test(walker.currentNode.textContent.trim())) timeEls.push(walker.currentNode.parentElement);
}
if (timeEls.length === 0) return {count: 0};
// 时间元素的最近公共祖先：向上走直到该祖先包含 >1 个时间
let container = timeEls[0];
while (container) {
  let cnt = 0;
  const w2 = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  while (w2.nextNode()) if (/^\\d{1,2}:\\d{2}$/.test(w2.currentNode.textContent.trim())) cnt++;
  if (cnt > 1) break;
  container = container.parentElement;
}
return {count: timeEls.length, html: container ? container.outerHTML.slice(0, 10000) : ''};
"""

try:
    for cid in ["52518", "37500", "60056", "77973"]:
        driver.get(f"https://dianying.taobao.com/cinemaDetail.htm?cinemaId={cid}&n_s=new")
        time.sleep(6)
        # 滚动到底触发懒加载
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(4)
        driver.execute_script("window.scrollTo(0, 400);")
        time.sleep(3)

        dump = driver.execute_script(JS_FIND)
        print(f"\ncinemaId={cid}: 时间元素 {dump['count']} 个")
        if dump["count"] > 0:
            with open("/workspace/sched_structure.html", "w") as f:
                f.write(dump.get("html") or "")
            print("已保存 sched_structure.html")
            print((dump.get("html") or "")[:4000])
            break
finally:
    driver.quit()
