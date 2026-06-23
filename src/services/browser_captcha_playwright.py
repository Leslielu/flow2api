"""
基于 Playwright + 真 Chrome + 固定 Profile 的 reCAPTCHA 打码服务

设计目标：
- 用固定 profile（含真实 Google auth 登录态，从 aime chrome_bot_profile 复制）
  + 真 Chrome（channel=chrome）拿到高评分 reCAPTCHA token，解决 nodriver 临时
  profile 被 Google 判为 PUBLIC_ERROR_UNUSUAL_ACTIVITY 的问题。
- 与内置打码（personal/browser）并列，作为 captcha_method = "playwright" 的独立方式。

参照：
- Drission 分支 browser_captcha_drission.py（单例 + 常驻页 + CDP 取 token + 心跳）
- aime src/skills/_browser_helpers/browser_manager.py（launch_persistent_context + channel=chrome + 反检测）

精简聚焦：只做取 token + 基础心跳（探活 / 页面健康 / 1 小时定时刷新）。
Session/Access Token 刷新继续由 token_manager 负责，不在此重复。
"""
import os
import asyncio
import time
import sqlite3
from typing import Optional, Dict, Any, List

from ..core.logger import debug_logger


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except Exception:
        pass
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()

# ==================== 依赖检测 ====================
try:
    from playwright.async_api import async_playwright, Error as PlaywrightError
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightError = Exception


# ==================== 常量 ====================
WEBSITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
HEARTBEAT_INTERVAL = 600        # 10 分钟探活
PAGE_REFRESH_INTERVAL = 3600    # 1 小时定时刷新常驻页
RECAPTCHA_SOLVE_TIMEOUT_MS = 45000
PAGE_LOAD_TIMEOUT_MS = 60000
PROFILE_DIR_NAME = "browser_data_playwright"


class PlaywrightCaptchaService:
    """Playwright + 真 Chrome + 固定 Profile 的 reCAPTCHA 打码服务（单例）"""

    _instance: Optional['PlaywrightCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self.website_key = WEBSITE_KEY
        self.user_data_dir = os.path.join(os.getcwd(), PROFILE_DIR_NAME)

        # Playwright 运行态
        self._playwright = None
        self._context = None
        self._initialized = False

        # 常驻页（单页复用模型：reCAPTCHA token 与 project_id 无关，同一 site key）
        self._resident_page = None
        self._resident_project_id: Optional[str] = None
        self._resident_lock = asyncio.Lock()       # 保护常驻页创建/重建
        self._solve_lock = asyncio.Lock()          # 串行化同页 evaluate

        # 心跳
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_page_refresh_at = 0.0

        # 缓存最近一次启动用的代理，用于 reload 时判断是否需要重启
        self._active_proxy_url: Optional[str] = None

    # ==================== 单例 ====================
    @classmethod
    async def get_instance(cls, db=None) -> 'PlaywrightCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    @classmethod
    async def reset_instance(cls):
        """销毁单例（测试/重启用）"""
        if cls._instance is not None:
            await cls._instance.close()
        cls._instance = None

    # ==================== 可用性 / 配置 ====================
    def _check_available(self):
        if IS_DOCKER:
            raise RuntimeError(
                "Playwright 打码在 Docker 环境中不可用。"
                "请使用第三方打码服务: yescaptcha, capmonster, ezcaptcha, capsolver"
            )
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright 未安装或不可用。请手动安装: pip install playwright"
            )

    def _get_proxy_url(self) -> Optional[str]:
        """从 captcha_config 表读取代理配置（复用现有 browser_proxy_* 字段）"""
        try:
            db_path = os.path.join(os.getcwd(), "data", "flow.db")
            if not os.path.exists(db_path):
                return None
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT browser_proxy_enabled, browser_proxy_url FROM captcha_config WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            if row and row[0] and row[1]:
                return row[1]
        except Exception as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 获取代理配置失败: {e}")
        return None

    # ==================== 启动 / 关闭 ====================
    async def initialize(self):
        """启动 Playwright + 真 Chrome（固定 profile）"""
        async with self._lock:
            self._check_available()

            if self._initialized and self._context is not None:
                # 已启动：做一次轻量探活
                if await self._is_context_alive():
                    return
                debug_logger.log_warning("[PlaywrightCaptcha] 浏览器已断开，重新初始化...")
                await self._cleanup_runtime()

            try:
                os.makedirs(self.user_data_dir, exist_ok=True)
                proxy_url = self._get_proxy_url()
                self._active_proxy_url = proxy_url

                debug_logger.log_info(
                    f"[PlaywrightCaptcha] 正在启动 Playwright + Chrome "
                    f"(profile={self.user_data_dir}, channel=chrome, "
                    f"proxy={proxy_url or '直连'})..."
                )

                self._playwright = await async_playwright().start()
                self._context = await self._launch_context(proxy_url)

                self._initialized = True
                debug_logger.log_info(
                    f"[PlaywrightCaptcha] ✅ 浏览器已启动 (Profile: {self.user_data_dir})"
                )

                self._start_heartbeat()
            except Exception as e:
                debug_logger.log_error(f"[PlaywrightCaptcha] ❌ 浏览器启动失败: {e}")
                await self._cleanup_runtime()
                raise

    async def _launch_context(self, proxy_url: Optional[str]):
        """启动持久化 context（参照 aime browser_manager.py 的反检测配置）"""
        launch_kwargs: Dict[str, Any] = dict(
            user_data_dir=self.user_data_dir,
            headless=False,                 # 有头，反检测（远端 Mac 有显示器/虚拟显示）
            channel="chrome",               # 真 Chrome，非 Chromium（关键，aime 同款）
            ignore_https_errors=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport=None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--mute-audio",
            ],
            ignore_default_args=["--enable-automation"],
        )
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        # 反检测 init script（参照 aime）：隐藏自动化特征
        await context.add_init_script(_ANTIDETECT_INIT_SCRIPT)
        return context

    async def _cleanup_runtime(self):
        """清理 Playwright 运行态（不抛异常）"""
        self._stop_heartbeat()
        try:
            if self._context is not None:
                await self._context.close()
        except Exception as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 关闭 context 异常: {e}")
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 停止 playwright 异常: {e}")
        self._context = None
        self._playwright = None
        self._resident_page = None
        self._resident_project_id = None
        self._initialized = False

    async def close(self):
        await self._cleanup_runtime()

    # ==================== 常驻页 ====================
    async def warmup_resident_tabs(self, project_ids: Optional[List[str]] = None, limit: int = 1) -> List[str]:
        """启动时预热常驻标签页（对齐 main.py 统一启动接口）。

        单常驻页模型：取第一个 project_id 建立常驻页即可（token 与 project_id 无关）。
        """
        await self.initialize()

        normalized = [str(p).strip() for p in (project_ids or []) if str(p).strip()]
        if not normalized:
            debug_logger.log_warning("[PlaywrightCaptcha] warmup 未提供 project_id，跳过常驻页预热")
            return []

        project_id = normalized[0]
        page = await self._ensure_resident_page(project_id)
        if page is not None:
            debug_logger.log_info(
                f"[PlaywrightCaptcha] ✅ 常驻页已预热 (project: {project_id})"
            )
            self._last_page_refresh_at = time.time()
            return [project_id]
        debug_logger.log_warning(f"[PlaywrightCaptcha] 常驻页预热失败 (project: {project_id})")
        return []

    async def _ensure_resident_page(self, project_id: str):
        """确保常驻页存在且健康；不健康则重建。返回 page 或 None。"""
        async with self._resident_lock:
            # 已有且健康 → 直接复用
            if self._resident_page is not None and not self._resident_page.is_closed():
                if await self._is_page_healthy(self._resident_page, require_recaptcha=False):
                    return self._resident_page
                debug_logger.log_warning("[PlaywrightCaptcha] 常驻页不健康，重建...")
            try:
                page = await self._create_resident_page(project_id)
                self._resident_page = page
                self._resident_project_id = project_id
                return page
            except Exception as e:
                debug_logger.log_error(f"[PlaywrightCaptcha] 创建常驻页失败: {e}")
                return None

    async def _create_resident_page(self, project_id: str):
        """新建常驻页：导航到 flow 项目页 + 确保 reCAPTCHA 就绪。"""
        # persistent context 启动后默认会有一个空白页，优先复用
        if self._context is None:
            raise RuntimeError("浏览器 context 未初始化")
        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()

        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        debug_logger.log_info(f"[PlaywrightCaptcha] 常驻页导航: {website_url}")
        try:
            await page.goto(website_url, wait_until="domcontentloaded",
                            timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightError as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 导航异常（继续尝试）: {e}")

        # 确保 reCAPTCHA 就绪（页面通常自动加载；未就绪则兜底注入 enterprise.js）
        await self._ensure_recaptcha_ready(page)
        return page

    async def _ensure_recaptcha_ready(self, page, timeout_ms: int = 20000):
        """等待 grecaptcha.enterprise 就绪；超时则注入 enterprise.js 兜底再等待。"""
        ready_js = (
            "() => typeof grecaptcha !== 'undefined' && "
            "typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        )
        deadline = time.time() + timeout_ms / 1000
        # 1) 先等页面自然加载的 reCAPTCHA
        while time.time() < deadline:
            try:
                if await page.evaluate(ready_js):
                    debug_logger.log_info("[PlaywrightCaptcha] reCAPTCHA 已就绪（页面自带）")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        # 2) 兜底：注入远程 enterprise.js
        debug_logger.log_warning("[PlaywrightCaptcha] 页面未自带 reCAPTCHA，注入 enterprise.js 兜底")
        try:
            await page.add_script_tag(
                url=f"https://www.google.com/recaptcha/enterprise.js?render={self.website_key}"
            )
        except Exception as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 注入 enterprise.js 失败: {e}")

        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                if await page.evaluate(ready_js):
                    debug_logger.log_info("[PlaywrightCaptcha] reCAPTCHA 已就绪（兜底注入）")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        debug_logger.log_error("[PlaywrightCaptcha] reCAPTCHA 始终未就绪")
        return False

    # ==================== 取 token（核心）====================
    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """获取 reCAPTCHA token。

        Returns: (token, browser_ref) —— browser_ref 固定为 "playwright"。
        """
        await self.initialize()

        # 用传入 project_id 取/建常驻页（若已有别的 project 常驻页也直接复用，token 同 site key）
        effective_pid = project_id or self._resident_project_id or "default"
        page = await self._ensure_resident_page(effective_pid)
        if page is None:
            debug_logger.log_error("[PlaywrightCaptcha] 无可用常驻页，取 token 失败")
            return None, None

        async with self._solve_lock:
            token = await self._execute_recaptcha_on_page(page, action)
        if token:
            debug_logger.log_info(
                f"[PlaywrightCaptcha] ✅ token 获取成功 (action={action}, len={len(token)})"
            )
            return token, "playwright"
        debug_logger.log_error(f"[PlaywrightCaptcha] ❌ token 获取失败 (action={action})")
        return None, None

    async def _execute_recaptcha_on_page(self, page, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """在常驻页执行 grecaptcha.enterprise.execute 取 token（JS 与 Drission 等价）。"""
        import json as _json
        solve_timeout_ms = RECAPTCHA_SOLVE_TIMEOUT_MS
        async_script = f"""
        (async () => {{
            const finishError = (error) => {{
                const message = error && error.message ? error.message : String(error || 'execute failed');
                return {{ ok: false, error: message }};
            }};
            try {{
                if (typeof grecaptcha === 'undefined' || typeof grecaptcha.enterprise === 'undefined') {{
                    return {{ ok: false, error: 'grecaptcha not defined' }};
                }}
                const token = await new Promise((resolve, reject) => {{
                    let settled = false;
                    const done = (handler, value) => {{
                        if (settled) return;
                        settled = true;
                        handler(value);
                    }};
                    const timer = setTimeout(() => {{
                        done(reject, new Error('execute timeout'));
                    }}, {solve_timeout_ms});
                    try {{
                        grecaptcha.enterprise.ready(() => {{
                            grecaptcha.enterprise.execute({_json.dumps(self.website_key)}, {{action: {_json.dumps(action)}}})
                                .then((token) => {{ clearTimeout(timer); done(resolve, token); }})
                                .catch((error) => {{ clearTimeout(timer); done(reject, error); }});
                        }});
                    }} catch (error) {{
                        clearTimeout(timer);
                        done(reject, error);
                    }}
                }});
                return {{ ok: true, token }};
            }} catch (error) {{
                return finishError(error);
            }}
        }})()
        """
        try:
            result = await page.evaluate(async_script)
        except Exception as e:
            debug_logger.log_error(f"[PlaywrightCaptcha] evaluate 异常: {e}")
            return None

        if not isinstance(result, dict):
            debug_logger.log_error(f"[PlaywrightCaptcha] evaluate 返回格式异常: {result}")
            return None
        if not result.get("ok"):
            debug_logger.log_error(f"[PlaywrightCaptcha] reCAPTCHA 错误: {result.get('error')}")
            return None
        token = result.get("token")
        return token if token else None

    # ==================== 心跳 ====================
    def _start_heartbeat(self):
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            debug_logger.log_info(
                f"[PlaywrightCaptcha] 心跳任务已启动（间隔 {HEARTBEAT_INTERVAL}s，"
                f"定时刷新 {PAGE_REFRESH_INTERVAL}s）"
            )

    def _stop_heartbeat(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            debug_logger.log_info("[PlaywrightCaptcha] 心跳任务已停止")
        self._heartbeat_task = None

    async def _heartbeat_loop(self):
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

                # 1) 浏览器存活
                if not await self._is_context_alive():
                    debug_logger.log_error("[PlaywrightCaptcha] ❌ Chrome 浏览器已断开！")
                    self._initialized = False
                    break

                debug_logger.log_info(
                    f"[PlaywrightCaptcha] ♥ 心跳成功（常驻页: "
                    f"{'有' if self._resident_page else '无'}）"
                )

                # 2) 常驻页健康检查，异常则自愈刷新
                if self._resident_page is not None:
                    healthy = await self._is_page_healthy(
                        self._resident_page, require_recaptcha=True
                    )
                    if not healthy:
                        debug_logger.log_warning("[PlaywrightCaptcha] 探活发现常驻页异常，触发刷新")
                        await self._refresh_resident_page()

                # 3) 每小时定时刷新一次
                now = time.time()
                if self._resident_page is not None and \
                        (now - self._last_page_refresh_at >= PAGE_REFRESH_INTERVAL):
                    debug_logger.log_info("[PlaywrightCaptcha] 到点定时刷新常驻页")
                    await self._refresh_resident_page()
                    self._last_page_refresh_at = now

            except asyncio.CancelledError:
                debug_logger.log_info("[PlaywrightCaptcha] 心跳任务被取消")
                break
            except Exception as e:
                debug_logger.log_error(f"[PlaywrightCaptcha] 心跳循环异常: {e}")

    async def _refresh_resident_page(self):
        """刷新常驻页（reload + 重新确保 reCAPTCHA 就绪）"""
        page = self._resident_page
        project_id = self._resident_project_id or "default"
        if page is None or page.is_closed():
            await self._ensure_resident_page(project_id)
            return
        try:
            await page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await self._ensure_recaptcha_ready(page)
            debug_logger.log_info("[PlaywrightCaptcha] 常驻页已刷新")
        except Exception as e:
            debug_logger.log_warning(f"[PlaywrightCaptcha] 刷新失败，重建常驻页: {e}")
            await self._ensure_resident_page(project_id)

    async def _is_context_alive(self) -> bool:
        if self._context is None:
            return False
        try:
            # persistent context 无 browser 对象，用 pages 探活
            _ = self._context.pages
            return True
        except Exception:
            return False

    async def _is_page_healthy(self, page, require_recaptcha: bool = True) -> bool:
        if page is None or page.is_closed():
            return False
        try:
            url = page.url or ""
            if "labs.google" not in url:
                return False
            if not require_recaptcha:
                return True
            ready = await page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && "
                "typeof grecaptcha.enterprise !== 'undefined'"
            )
            return bool(ready)
        except Exception:
            return False

    # ==================== admin 热重载 / 分数测试 ====================
    async def reload_config(self):
        """admin 热重载：若代理配置变化则重启浏览器，否则仅记录。"""
        new_proxy = self._get_proxy_url()
        if new_proxy != self._active_proxy_url:
            debug_logger.log_info(
                f"[PlaywrightCaptcha] 代理变更 ({self._active_proxy_url} -> {new_proxy})，重启浏览器"
            )
            await self._cleanup_runtime()
            await self.initialize()
        else:
            debug_logger.log_info("[PlaywrightCaptcha] reload_config: 配置未变，无需重启")

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "IMAGE_GENERATION",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """admin 分数测试入口：直接复用常驻页取一次 token 返回。"""
        try:
            token, _ = await self.get_token(self._resident_project_id or "default", action=action)
            return {
                "supported": True,
                "token": token,
                "length": len(token) if token else 0,
                "message": "OK" if token else "取 token 失败",
            }
        except Exception as e:
            return {"supported": True, "token": None, "length": 0, "message": str(e)}


# ==================== 反检测 init script（参照 aime browser_manager.py）====================
_ANTIDETECT_INIT_SCRIPT = """
() => {
    // 1. 覆盖 webdriver 属性
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch (e) {}

    // 2. 伪装 Chrome 插件列表
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
                ];
                plugins.item = (i) => plugins[i] || null;
                plugins.namedItem = (name) => plugins.find(p => p.name === name) || null;
                plugins.refresh = () => {};
                return plugins;
            }
        });
    } catch (e) {}

    // 3. 伪装语言列表
    try {
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
    } catch (e) {}

    // 4. 隐藏 Automation 相关属性
    try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; } catch (e) {}
    try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; } catch (e) {}
    try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol; } catch (e) {}

    // 5. 修复 permissions 查询
    try {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    } catch (e) {}
}
"""
