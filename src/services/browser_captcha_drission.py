"""
浏览器自动化获取 reCAPTCHA token
使用 DrissionPage 实现反检测浏览器
支持常驻模式：为每个 project_id 自动创建常驻标签页，即时生成 token
"""
import asyncio
import time
import os
import sys
import subprocess
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from ..core.logger import debug_logger


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


# ==================== DrissionPage 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令

    Args:
        package: 包名
        use_mirror: 是否使用国内镜像

    Returns:
        是否安装成功
    """
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])

    try:
        debug_logger.log_info(f"[DrissionCaptcha] 正在安装 {package}...")
        print(f"[DrissionCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[DrissionCaptcha] ✅ {package} 安装成功")
            print(f"[DrissionCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[DrissionCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[DrissionCaptcha] {package} 安装异常: {e}")
        return False


def _ensure_drissionpage_installed() -> bool:
    """确保 DrissionPage 已安装

    Returns:
        是否安装成功/已安装
    """
    try:
        import DrissionPage
        debug_logger.log_info("[DrissionCaptcha] DrissionPage 已安装")
        return True
    except ImportError:
        pass

    debug_logger.log_info("[DrissionCaptcha] DrissionPage 未安装，开始自动安装...")
    print("[DrissionCaptcha] DrissionPage 未安装，开始自动安装...")

    # 先尝试官方源
    if _run_pip_install('DrissionPage', use_mirror=False):
        return True

    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[DrissionCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[DrissionCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('DrissionPage', use_mirror=True):
        return True

    debug_logger.log_error("[DrissionCaptcha] ❌ DrissionPage 自动安装失败，请手动安装: pip install DrissionPage")
    print("[DrissionCaptcha] ❌ DrissionPage 自动安装失败，请手动安装: pip install DrissionPage")
    return False


# 尝试导入 DrissionPage
ChromiumPage = None
DRISSIONPAGE_AVAILABLE = False

if IS_DOCKER:
    debug_logger.log_warning("[DrissionCaptcha] 检测到 Docker 环境，内置浏览器打码不可用，请使用第三方打码服务")
    print("[DrissionCaptcha] ⚠️ 检测到 Docker 环境，内置浏览器打码不可用")
    print("[DrissionCaptcha] 请使用第三方打码服务: yescaptcha, capmonster, ezcaptcha, capsolver")
else:
    if _ensure_drissionpage_installed():
        try:
            from DrissionPage import ChromiumPage
            # Tab 是 ChromiumPage.new_tab() 返回的对象，不需要单独导入
            DRISSIONPAGE_AVAILABLE = True
        except ImportError as e:
            debug_logger.log_error(f"[DrissionCaptcha] DrissionPage 导入失败: {e}")
            print(f"[DrissionCaptcha] ❌ DrissionPage 导入失败: {e}")


# ==================== 线程池用于同步调用 ====================
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="drission_")


def _run_in_thread(func, *args, **kwargs):
    """在线程池中运行同步函数"""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_executor, func, *args, **kwargs)


# ==================== 心跳配置 ====================
HEARTBEAT_INTERVAL = 600  # 10分钟 = 600秒
PAGE_REFRESH_INTERVAL = 3500  # 约58分钟20秒
TELEGRAM_API_URL = "http://localhost:5678/telegram/send"


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(self, tab, project_id: str):
        self.tab = tab
        self.project_id = project_id
        self.recaptcha_ready = False
        self.created_at = time.time()


class DrissionCaptchaService:
    """DrissionPage 浏览器自动化获取 reCAPTCHA token

    支持两种模式：
    1. 常驻模式 (Resident Mode): 为每个 project_id 保持常驻标签页，即时生成 token
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['DrissionCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.browser: Optional[ChromiumPage] = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        # 持久化 profile 目录（与 nodriver 使用不同的目录）
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data_drission")
        # Cookies 持久化文件（解决 macOS 上 Chrome 不保存 cookies 的问题）
        self.cookies_file = os.path.join(os.getcwd(), "browser_data_drission", "saved_cookies.json")

        # 常驻模式相关属性 (支持多 project_id)
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # project_id -> 常驻标签页信息
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作

        # 兼容旧 API（保留 single resident 属性作为别名）
        self.resident_project_id: Optional[str] = None  # 向后兼容
        self.resident_tab = None                         # 向后兼容
        self._running = False                            # 向后兼容
        self._recaptcha_ready = False                    # 向后兼容

        # 心跳任务
        self._heartbeat_task: Optional[asyncio.Task] = None
        # 页面健康检查/定时刷新时间戳
        self._last_page_refresh_at = 0.0

    @classmethod
    async def get_instance(cls, db=None) -> 'DrissionCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    def _check_available(self):
        """检查服务是否可用"""
        if IS_DOCKER:
            raise RuntimeError(
                "内置浏览器打码在 Docker 环境中不可用。"
                "请使用第三方打码服务: yescaptcha, capmonster, ezcaptcha, capsolver"
            )
        if not DRISSIONPAGE_AVAILABLE or ChromiumPage is None:
            raise RuntimeError(
                "DrissionPage 未安装或不可用。"
                "请手动安装: pip install DrissionPage"
            )

    # ==================== 同步方法（在线程池中运行）====================

    def _sync_create_browser(self):
        """同步创建浏览器实例"""
        from DrissionPage import ChromiumOptions

        # 确保 user_data_dir 存在
        os.makedirs(self.user_data_dir, exist_ok=True)

        # 配置浏览器选项
        co = ChromiumOptions()
        # 不使用 auto_port，手动设置本地端口以避免临时目录
        co.set_local_port(9222)
        # 设置用户数据目录（使用 DrissionPage 的专用方法）
        co.set_user_data_path(self.user_data_dir)
        # 只使用最基本的参数
        co.set_argument('--window-size=1280,720')

        # 配置代理（如果启用）
        proxy_url = self._get_proxy_url()
        if proxy_url:
            co.set_proxy(proxy_url)
            debug_logger.log_info(f"[DrissionCaptcha] 使用代理: {proxy_url}")

        debug_logger.log_info(f"[DrissionCaptcha] 正在启动 DrissionPage 浏览器 (用户数据目录: {self.user_data_dir})...")

        # 创建浏览器
        browser = ChromiumPage(addr_or_opts=co)
        self.browser = browser  # 临时设置，用于加载 cookies

        # 尝试从文件加载 cookies
        self._sync_load_cookies()

        debug_logger.log_info(f"[DrissionCaptcha] ✅ DrissionPage 浏览器已启动 (Profile: {self.user_data_dir})")
        return browser

    def _get_proxy_url(self) -> Optional[str]:
        """获取代理 URL（从数据库配置）"""
        if self.db is None:
            return None
        try:
            # 同步获取代理配置 - 直接从数据库读取
            import sqlite3
            db_path = os.path.join(os.getcwd(), "data", "flow.db")
            if not os.path.exists(db_path):
                return None
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT browser_proxy_enabled, browser_proxy_url FROM captcha_config WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            if row and row[0] and row[1]:  # browser_proxy_enabled and browser_proxy_url
                return row[1]
        except Exception as e:
            debug_logger.log_warning(f"[DrissionCaptcha] 获取代理配置失败: {e}")
        return None

    def _sync_check_browser_alive(self):
        """同步检查浏览器是否存活"""
        if self.browser is None:
            return False
        try:
            # 尝试访问 url 属性来检查浏览器是否存活
            _ = self.browser.url
            return True
        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 浏览器存活检查失败: {type(e).__name__}: {str(e)[:100]}")
            return False

    def _sync_new_tab(self, url: str):
        """同步创建新标签页"""
        return self.browser.new_tab(url)

    def _sync_get_tab(self, url: str):
        """同步访问页面（当前标签页）"""
        self.browser.get(url)
        return self.browser

    def _sync_tab_get(self, tab, url: str):
        """同步在指定标签页上访问页面"""
        tab.get(url)

    def _sync_close_tab(self, tab):
        """同步关闭标签页"""
        try:
            tab.close()
        except:
            pass

    def _sync_save_cookies(self):
        """保存 cookies 到文件（解决 macOS 上 cookies 不持久化的问题）"""
        try:
            if self.browser:
                import json
                cookies = self.browser.cookies()
                os.makedirs(os.path.dirname(self.cookies_file), exist_ok=True)
                with open(self.cookies_file, 'w') as f:
                    json.dump(cookies, f)
                debug_logger.log_info(f"[DrissionCaptcha] 已保存 {len(cookies)} 个 cookies 到文件")
        except Exception as e:
            debug_logger.log_warning(f"[DrissionCaptcha] 保存 cookies 失败: {e}")

    def _sync_load_cookies(self):
        """从文件加载 cookies 并注入浏览器"""
        try:
            import json
            if os.path.exists(self.cookies_file) and self.browser:
                with open(self.cookies_file, 'r') as f:
                    cookies = json.load(f)
                # 先访问目标域名，然后注入 cookies
                for cookie in cookies:
                    try:
                        self.browser.set.cookies(cookie)
                    except:
                        pass
                debug_logger.log_info(f"[DrissionCaptcha] 已从文件加载 {len(cookies)} 个 cookies")
                return True
        except Exception as e:
            debug_logger.log_warning(f"[DrissionCaptcha] 加载 cookies 失败: {e}")
        return False

    def _sync_close_browser(self):
        """同步关闭浏览器"""
        try:
            if self.browser:
                # 关闭前保存 cookies
                self._sync_save_cookies()
                self.browser.quit()
        except:
            pass

    def _sync_reload_tab(self, tab):
        """同步刷新标签页"""
        tab.refresh()

    def _sync_heartbeat_js(self, tab) -> bool:
        """同步执行心跳 JS，返回是否成功"""
        try:
            tab.run_js("Date.now()")
            return True
        except:
            return False

    def _sync_get_cookies(self):
        """同步获取所有 cookies"""
        return self.browser.cookies()

    def _extract_session_token_from_cookies(self, cookies) -> Optional[str]:
        """从不同格式的 cookies 容器中提取 session token"""
        target = "__Secure-next-auth.session-token"
        if not cookies:
            return None

        # 格式1: {cookie_name: cookie_value}
        if isinstance(cookies, dict):
            value = cookies.get(target)
            if value:
                return value

        # 格式2: 带 as_dict()/to_dict() 的 cookies 对象
        for method_name in ("as_dict", "to_dict"):
            method = getattr(cookies, method_name, None)
            if callable(method):
                try:
                    data = method()
                    if isinstance(data, dict):
                        value = data.get(target)
                        if value:
                            return value
                except Exception:
                    pass

        # 格式3: 支持 get(name)
        getter = getattr(cookies, "get", None)
        if callable(getter):
            try:
                value = getter(target)
                if value:
                    return value
            except Exception:
                pass

        # 格式4: 可迭代 cookies（list/CookiesList/对象列表）
        try:
            iterator = iter(cookies)
        except TypeError:
            return None

        for cookie in iterator:
            if isinstance(cookie, dict):
                if cookie.get("name") == target:
                    value = cookie.get("value")
                    if value:
                        return value
                value = cookie.get(target)
                if value:
                    return value
            elif isinstance(cookie, (tuple, list)) and len(cookie) >= 2:
                if cookie[0] == target and cookie[1]:
                    return cookie[1]
            else:
                name = getattr(cookie, "name", None)
                if name == target:
                    value = getattr(cookie, "value", None)
                    if value:
                        return value

        return None

    def _sync_is_resident_page_healthy(self, tab, project_id: str) -> bool:
        """同步检查常驻标签页是否健康（URL/DOM/reCAPTCHA）"""
        try:
            current_url = tab.url if hasattr(tab, 'url') else ''
            expected_part = f"/tools/flow/project/{project_id}"
            if not current_url or expected_part not in current_url:
                return False

            check_js = """
            return (function() {
                try {
                    const hasDocument = typeof document !== 'undefined';
                    if (!hasDocument) return false;
                    const ready = document.readyState === 'complete' || document.readyState === 'interactive';
                    const hasBody = !!document.body;
                    const hasRecaptcha = typeof grecaptcha !== 'undefined' &&
                        typeof grecaptcha.enterprise !== 'undefined';
                    return ready && hasBody && hasRecaptcha;
                } catch (e) {
                    return false;
                }
            })();
            """
            js_ok = tab.run_js(check_js)
            return bool(js_ok)
        except Exception:
            return False

    # ==================== 心跳机制 ====================

    async def _heartbeat_loop(self):
        """后台心跳循环：每10分钟探活，页面异常时自愈刷新；每1小时定时刷新一次"""
        debug_logger.log_info(f"[DrissionCaptcha] 心跳任务已启动（间隔 {HEARTBEAT_INTERVAL} 秒）")

        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

                # 1) 检查浏览器进程存活
                is_alive = await _run_in_thread(self._sync_check_browser_alive)
                if not is_alive:
                    debug_logger.log_error("[DrissionCaptcha] ❌ Chrome 浏览器已停止运行！")
                    await self._send_telegram_alert("⚠️ flow2api: Chrome 浏览器已停止运行，需要重启服务")
                    # 标记未初始化，下次调用会重新启动
                    self._initialized = False
                    self.browser = None
                    break

                # 2) 心跳成功日志
                debug_logger.log_info(f"[DrissionCaptcha] ♥ 心跳成功（常驻标签页: {len(self._resident_tabs)} 个）")

                # 3) 页面健康检查（无常驻页则跳过）
                page_ok = await self._check_resident_pages_health()
                if not page_ok:
                    debug_logger.log_warning("[DrissionCaptcha] 探活发现页面异常，已触发刷新")

                # 4) 每小时定时刷新一次常驻页
                now = time.time()
                if self._resident_tabs and (now - self._last_page_refresh_at >= PAGE_REFRESH_INTERVAL):
                    refresh_ok = await self._refresh_all_resident_tabs("定时刷新")
                    if refresh_ok:
                        self._last_page_refresh_at = now

                # 5) 心跳后检查并更新 Session Token
                await self._check_and_update_session_tokens()

                # 6) 心跳后检查并刷新 Access Token（AT）
                await self._check_and_refresh_access_tokens()

            except asyncio.CancelledError:
                debug_logger.log_info("[DrissionCaptcha] 心跳任务被取消")
                break
            except Exception as e:
                debug_logger.log_error(f"[DrissionCaptcha] 心跳循环异常: {e}")
                await self._send_telegram_alert(f"⚠️ flow2api: Chrome 心跳检测异常: {e}")

    async def _refresh_all_resident_tabs(self, reason: str) -> bool:
        """刷新所有常驻标签页并等待加载完成"""
        if not self._resident_tabs:
            return True

        all_ok = True
        for project_id, resident_info in list(self._resident_tabs.items()):
            try:
                if not resident_info or not resident_info.tab:
                    all_ok = False
                    continue
                debug_logger.log_info(f"[DrissionCaptcha] [{reason}] 刷新常驻页 project={project_id}")
                await _run_in_thread(self._sync_reload_tab, resident_info.tab)
                page_loaded = await self._wait_page_load(resident_info.tab, timeout=30)
                if not page_loaded:
                    all_ok = False
                    debug_logger.log_warning(f"[DrissionCaptcha] [{reason}] 刷新后页面仍未加载: project={project_id}")
            except Exception as e:
                all_ok = False
                debug_logger.log_warning(f"[DrissionCaptcha] [{reason}] 刷新失败 project={project_id}: {e}")
        return all_ok

    async def _check_resident_pages_health(self) -> bool:
        """检查常驻页健康；若异常则尝试刷新一次自愈"""
        if not self._resident_tabs:
            return True

        unhealthy_projects = []
        for project_id, resident_info in list(self._resident_tabs.items()):
            try:
                if not resident_info or not resident_info.tab:
                    unhealthy_projects.append(project_id)
                    continue
                healthy = await _run_in_thread(
                    self._sync_is_resident_page_healthy,
                    resident_info.tab,
                    project_id,
                )
                if not healthy:
                    unhealthy_projects.append(project_id)
            except Exception:
                unhealthy_projects.append(project_id)

        if not unhealthy_projects:
            return True

        debug_logger.log_warning(f"[DrissionCaptcha] 页面健康检查异常，准备刷新: {unhealthy_projects}")
        ok = await self._refresh_all_resident_tabs("探活自愈")
        if ok:
            self._last_page_refresh_at = time.time()
        return ok

    async def _check_and_update_session_tokens(self):
        """检查并更新 Session Token（心跳时调用）

        遍历所有常驻标签页，获取最新的 __Secure-next-auth.session-token，
        与数据库中的对比，如果变化则自动更新。
        """
        if not self._resident_tabs:
            return

        try:
            # 遍历所有常驻标签页，从各自的 tab 获取 cookies 并检查 session token
            for project_id, resident_info in list(self._resident_tabs.items()):
                try:
                    tab = resident_info.tab if resident_info else None
                    if not tab:
                        continue

                    # 从标签页级别获取 cookies（而非 browser 级别）
                    cookies = await _run_in_thread(tab.cookies)
                    if not cookies:
                        debug_logger.log_warning(f"[DrissionCaptcha] 常驻标签页 {project_id} 无法获取 cookies，跳过")
                        continue

                    # 提取 session token（兼容 dict / list / CookiesList 等多种格式）
                    new_session_token = self._extract_session_token_from_cookies(cookies)
                    if not new_session_token:
                        continue

                    # 从数据库查询该 project_id 对应的 token
                    token = await self._get_token_by_project_id(project_id)
                    if not token:
                        continue

                    # 检查 session token 是否变化
                    st_changed = token.st != new_session_token
                    if st_changed:
                        debug_logger.log_info(f"[DrissionCaptcha] Token {token.id} ({token.email}) 的 Session Token 已更新")
                        await self._update_token_st(token.id, new_session_token)

                    # 如果 token 被禁用（非429原因），尝试自动恢复
                    if not token.is_active and token.ban_reason != "429_rate_limit":
                        st_for_recovery = new_session_token if st_changed else token.st
                        await self._try_recover_disabled_token(token.id, token.email, st_for_recovery)

                except Exception as e:
                    debug_logger.log_error(f"[DrissionCaptcha] 检查 project_id={project_id} 的 Session Token 失败: {e}")

        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 检查 Session Token 异常: {e}")

    async def _try_recover_disabled_token(self, token_id: int, email: str, st: str):
        """尝试恢复被禁用的 Token：用当前 ST 刷新 AT，成功则重新启用

        仅在心跳检查 Session Token 时调用，用于自动恢复因 AT/ST 刷新失败被禁用的 token。
        不会恢复因 429 被禁用的 token（那些由 auto_unban_429_tokens 处理）。

        Args:
            token_id: Token ID
            email: Token 邮箱（用于日志）
            st: 用于刷新 AT 的 Session Token
        """
        try:
            debug_logger.log_info(f"[AUTO_RECOVER] Token {token_id} ({email}): 尝试自动恢复...")

            from .token_manager import TokenManager
            tm = TokenManager(self.db)

            # 用 ST 尝试刷新 AT
            result = await tm._do_refresh_at(token_id, st)
            if result:
                await tm.enable_token(token_id)
                debug_logger.log_info(f"[AUTO_RECOVER] ✅ Token {token_id} ({email}) 自动恢复成功")
                await self._send_telegram_alert(f"✅ flow2api: Token {token_id} ({email}) 已自动恢复")
            else:
                debug_logger.log_warning(f"[AUTO_RECOVER] Token {token_id} ({email}): AT 刷新失败，保持禁用")

        except Exception as e:
            debug_logger.log_error(f"[AUTO_RECOVER] Token {token_id} ({email}): 自动恢复异常 - {e}")

    async def _check_and_refresh_access_tokens(self):
        """检查并刷新即将过期的 AT（心跳时调用）

        遍历所有活跃 token，发现 AT 过期或即将过期时主动刷新，
        刷新成功后重载该 token 对应的常驻标签页，使新 session 生效。
        """
        if not self.db:
            return
        try:
            from .token_manager import TokenManager
            tm = TokenManager(self.db)
            tokens = await self.db.get_active_tokens()
            for token in tokens:
                if not tm._should_refresh_at(token):
                    continue
                debug_logger.log_info(f"[DrissionCaptcha] [AT_CHECK] Token {token.id} AT 即将过期/已过期，心跳触发刷新...")
                refreshed = await tm._refresh_at(token.id)
                if refreshed:
                    debug_logger.log_info(f"[DrissionCaptcha] [AT_CHECK] Token {token.id} AT 刷新成功，重载对应常驻页...")
                    # 找出该 token 关联的所有常驻标签页并重载
                    for project_id, resident_info in list(self._resident_tabs.items()):
                        tab_token = await self._get_token_by_project_id(project_id)
                        if tab_token and tab_token.id == token.id and resident_info and resident_info.tab:
                            try:
                                debug_logger.log_info(f"[DrissionCaptcha] [AT_CHECK] 重载常驻页 project={project_id}")
                                await _run_in_thread(self._sync_reload_tab, resident_info.tab)
                                await self._wait_page_load(resident_info.tab, timeout=30)
                            except Exception as e:
                                debug_logger.log_warning(f"[DrissionCaptcha] [AT_CHECK] 重载常驻页失败 project={project_id}: {e}")
                else:
                    debug_logger.log_warning(f"[DrissionCaptcha] [AT_CHECK] Token {token.id} AT 刷新失败")
        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] AT 心跳检查异常: {e}")

    async def _get_token_by_project_id(self, project_id: str):
        """根据 project_id 查询对应的 Token

        Args:
            project_id: 项目 UUID

        Returns:
            Token 对象，如果未找到返回 None
        """
        try:
            import sqlite3
            db_path = os.path.join(os.getcwd(), "data", "flow.db")
            if not os.path.exists(db_path):
                return None

            # 使用线程池执行同步数据库查询
            def query_token():
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM tokens WHERE current_project_id = ?", (project_id,))
                row = cursor.fetchone()
                conn.close()
                if row:
                    from ..core.models import Token
                    return Token(**dict(row))
                return None

            return await _run_in_thread(query_token)
        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 查询 Token 失败: {e}")
            return None

    async def _update_token_st(self, token_id: int, new_st: str):
        """更新 Token 的 Session Token

        Args:
            token_id: Token ID
            new_st: 新的 Session Token
        """
        try:
            import sqlite3
            db_path = os.path.join(os.getcwd(), "data", "flow.db")

            def update_st():
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("UPDATE tokens SET st = ? WHERE id = ?", (new_st, token_id))
                conn.commit()
                conn.close()

            await _run_in_thread(update_st)
            debug_logger.log_info(f"[DrissionCaptcha] ✅ Token {token_id} 的 Session Token 已更新到数据库")
        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 更新 Token {token_id} 的 Session Token 失败: {e}")

    async def _send_telegram_alert(self, message: str):
        """发送 Telegram 告警"""
        try:
            import urllib.request
            import json

            data = json.dumps({"text": message}).encode('utf-8')
            req = urllib.request.Request(
                TELEGRAM_API_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method='POST'
            )

            # 使用线程池执行同步 HTTP 请求，避免阻塞
            def do_request():
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status
                except Exception as e:
                    return str(e)

            result = await _run_in_thread(do_request)

            if result == 200:
                debug_logger.log_info(f"[DrissionCaptcha] Telegram 告警已发送: {message}")
            else:
                debug_logger.log_warning(f"[DrissionCaptcha] Telegram 告警发送失败: {result}")

        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] Telegram 告警发送异常: {e}")

    def _start_heartbeat(self):
        """启动心跳任务"""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            debug_logger.log_info("[DrissionCaptcha] 心跳任务已调度")

    def _stop_heartbeat(self):
        """停止心跳任务"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
            debug_logger.log_info("[DrissionCaptcha] 心跳任务已停止")

    # ==================== 异步 API ====================

    async def initialize(self):
        """初始化 DrissionPage 浏览器"""
        # 使用锁防止并发初始化
        async with self._lock:
            # 检查服务是否可用
            self._check_available()

            if self._initialized and self.browser:
                # 检查浏览器是否仍然存活
                is_alive = await _run_in_thread(self._sync_check_browser_alive)
                if is_alive:
                    return
                else:
                    debug_logger.log_warning("[DrissionCaptcha] 浏览器已停止，重新初始化...")
                    self._initialized = False

            try:
                debug_logger.log_info("[DrissionCaptcha] 开始在线程池中创建浏览器...")
                # 在线程池中创建浏览器
                self.browser = await _run_in_thread(self._sync_create_browser)
                self._initialized = True
                debug_logger.log_info("[DrissionCaptcha] 浏览器创建完成，已设置 _initialized=True")

                # 启动心跳任务
                self._start_heartbeat()
            except Exception as e:
                debug_logger.log_error(f"[DrissionCaptcha] ❌ 浏览器启动失败: {str(e)}")
                raise

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式

        Args:
            project_id: 用于常驻的项目 ID
        """
        if self._running:
            debug_logger.log_warning("[DrissionCaptcha] 常驻模式已在运行")
            return

        await self.initialize()

        self.resident_project_id = project_id
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        debug_logger.log_info(f"[DrissionCaptcha] 启动常驻模式（预热导航）: {website_url}")

        # 启动阶段也走统一预热流程：google.com -> labs.google/fx/zh/tools/flow -> flow project
        resident_info = await self._create_resident_tab(project_id)
        if not resident_info:
            debug_logger.log_error("[DrissionCaptcha] 常驻模式启动失败：预热导航创建标签页失败")
            return

        # 向后兼容旧属性
        self.resident_tab = resident_info.tab
        self._recaptcha_ready = resident_info.recaptcha_ready
        self._resident_tabs[project_id] = resident_info
        self._running = True
        self._last_page_refresh_at = time.time()

        debug_logger.log_info(f"[DrissionCaptcha] ✅ 常驻模式已启动 (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """停止常驻模式

        Args:
            project_id: 指定要关闭的 project_id，如果为 None 则关闭所有常驻标签页
        """
        async with self._resident_lock:
            if project_id:
                # 关闭指定的常驻标签页
                await self._close_resident_tab(project_id)
                debug_logger.log_info(f"[DrissionCaptcha] 已关闭 project_id={project_id} 的常驻模式")
            else:
                # 关闭所有常驻标签页
                project_ids = list(self._resident_tabs.keys())
                for pid in project_ids:
                    resident_info = self._resident_tabs.pop(pid, None)
                    if resident_info and resident_info.tab:
                        await _run_in_thread(self._sync_close_tab, resident_info.tab)
                debug_logger.log_info(f"[DrissionCaptcha] 已关闭所有常驻标签页 (共 {len(project_ids)} 个)")

        # 向后兼容：清理旧属性
        if not self._running:
            return

        self._running = False
        if self.resident_tab:
            await _run_in_thread(self._sync_close_tab, self.resident_tab)
            self.resident_tab = None

        self.resident_project_id = None
        self._recaptcha_ready = False

    async def _wait_page_load(self, tab, timeout: int = 60) -> bool:
        """等待页面加载完成

        Args:
            tab: DrissionPage 标签页对象
            timeout: 超时时间（秒）

        Returns:
            True if 页面加载成功
        """
        for retry in range(timeout):
            try:
                await asyncio.sleep(1)
                # 使用 url 属性检查页面是否已加载（比 run_js 更稳定）
                current_url = await _run_in_thread(lambda: tab.url if hasattr(tab, 'url') else '')
                debug_logger.log_info(f"[DrissionCaptcha] 当前 URL: {current_url} (重试 {retry + 1}/{timeout})")
                # 如果 URL 包含目标域名，认为页面已加载
                if current_url and ('labs.google' in current_url or 'flow/project' in current_url):
                    return True
            except Exception as e:
                debug_logger.log_warning(f"[DrissionCaptcha] 等待页面异常: {e}，重试 {retry + 1}/{timeout}...")
                await asyncio.sleep(1)

        return False

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载

        Args:
            tab: DrissionPage 标签页对象

        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[DrissionCaptcha] 等待页面和 reCAPTCHA 加载...")

        # 简单等待一段时间，让页面自己加载 reCAPTCHA
        # Google Flow 页面会自动加载 reCAPTCHA
        await asyncio.sleep(5)

        # 不再使用 run_js 检测，直接返回 True
        # 在实际调用 execute 时会检测 reCAPTCHA 是否可用
        debug_logger.log_info("[DrissionCaptcha] 页面已加载，假定 reCAPTCHA 可用")
        return True

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token

        Args:
            tab: DrissionPage 标签页对象
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token 或 None
        """
        debug_logger.log_info(f"[DrissionCaptcha] 开始执行 reCAPTCHA (action: {action})...")

        # 先检查 grecaptcha 是否存在 (需要加 return 才能获取返回值)
        check_script = "return typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined'"
        has_recaptcha = await _run_in_thread(lambda: tab.run_js(check_script))
        debug_logger.log_info(f"[DrissionCaptcha] reCAPTCHA 检查结果: {has_recaptcha}")

        if not has_recaptcha:
            debug_logger.log_error(f"[DrissionCaptcha] 页面没有 reCAPTCHA")
            return None

        # DrissionPage run_js 不支持 async/await 返回值，使用全局变量方式
        ts = int(time.time() * 1000)
        token_var = f"_recaptcha_token_{ts}"
        status_var = f"_recaptcha_status_{ts}"

        # 注入脚本 - 使用标准回调方式
        script = f"""
        (function() {{
            window.{token_var} = null;
            window.{status_var} = 'waiting';

            if (typeof grecaptcha === 'undefined' || typeof grecaptcha.enterprise === 'undefined') {{
                window.{status_var} = 'error:grecaptcha not defined';
                return;
            }}

            grecaptcha.enterprise.ready(function() {{
                grecaptcha.enterprise.execute('{self.website_key}', {{action: '{action}'}})
                    .then(function(token) {{
                        window.{token_var} = token;
                        window.{status_var} = 'success';
                    }})
                    .catch(function(err) {{
                        window.{status_var} = 'error:' + (err.message || 'unknown');
                    }});
            }});
        }})()
        """

        # 执行注入脚本
        inject_result = await _run_in_thread(lambda: tab.run_js(script))
        debug_logger.log_info(f"[DrissionCaptcha] 注入脚本结果: {inject_result}")

        # 轮询等待结果（最多 20 秒）
        for i in range(40):
            await asyncio.sleep(0.5)

            # 检查状态 - 需要加 return 才能获取返回值
            try:
                status = await _run_in_thread(lambda: tab.run_js(f"return window.{status_var}"))
                if status is None:
                    status = await _run_in_thread(lambda: tab.run_js(f"return typeof window.{status_var} !== 'undefined' ? window.{status_var} : 'undefined'"))
            except:
                status = 'error:read_failed'

            debug_logger.log_info(f"[DrissionCaptcha] 状态检查 (重试 {i+1}/40): {status}")

            if status == 'success':
                # 获取 token - 需要加 return
                token = await _run_in_thread(lambda: tab.run_js(f"return window.{token_var}"))
                if token and (token.startswith('03') or len(token) > 500):
                    debug_logger.log_info(f"[DrissionCaptcha] ✅ Token 获取成功，长度: {len(token)}")
                    return token

            if status and isinstance(status, str) and status.startswith('error:'):
                debug_logger.log_error(f"[DrissionCaptcha] 错误: {status}")
                return None

        debug_logger.log_error(f"[DrissionCaptcha] 获取 token 超时")
        return None

    # ==================== 主要 API ====================

    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 reCAPTCHA token

        自动常驻模式：复用常驻标签页，直接执行 JS 获取 token，无需刷新页面

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已初始化
        await self.initialize()

        # 尝试从常驻标签页获取 token
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)

            # 如果该 project_id 没有常驻标签页，则自动创建
            if resident_info is None:
                debug_logger.log_info(f"[DrissionCaptcha] project_id={project_id} 没有常驻标签页，正在创建...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[DrissionCaptcha] 无法创建常驻标签页，fallback 到传统模式")
                    return await self._get_token_legacy(project_id, action)
                self._resident_tabs[project_id] = resident_info

        # 使用常驻标签页直接执行 JS 获取 token（不刷新页面）
        if resident_info and resident_info.recaptcha_ready and resident_info.tab:
            start_time = time.time()
            debug_logger.log_info(f"[DrissionCaptcha] 从常驻标签页即时生成 token (action: {action})...")
            try:
                token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    debug_logger.log_info(f"[DrissionCaptcha] ✅ Token生成成功（耗时 {duration_ms:.0f}ms）")
                    return token
                else:
                    debug_logger.log_warning(f"[DrissionCaptcha] 常驻标签页生成失败，尝试重建...")
            except Exception as e:
                debug_logger.log_warning(f"[DrissionCaptcha] 常驻标签页异常: {e}，尝试重建...")

            # 常驻标签页失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    try:
                        token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                        if token:
                            debug_logger.log_info(f"[DrissionCaptcha] ✅ 重建后 Token生成成功")
                            return token
                    except Exception:
                        pass

        # 最终 Fallback: 使用传统模式（刷新页面）
        debug_logger.log_warning(f"[DrissionCaptcha] 所有常驻方式失败，fallback 到传统模式")
        return await self._get_token_legacy(project_id, action)

    async def _get_token_legacy(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """传统模式获取 token（刷新页面，作为 fallback）"""
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        debug_logger.log_info(f"[DrissionCaptcha] [Legacy] 访问页面获取 token: {website_url}")

        await _run_in_thread(lambda: self.browser.get(website_url))

        # 等待页面加载完成
        for i in range(30):
            await asyncio.sleep(1)
            url = await _run_in_thread(lambda: self.browser.url)
            if url and ('labs.google' in url or 'flow/project' in url):
                break

        # 等待 reCAPTCHA 脚本加载
        await asyncio.sleep(5)

        token = await self._execute_recaptcha_on_tab(self.browser, action)
        if token:
            debug_logger.log_info(f"[DrissionCaptcha] [Legacy] ✅ Token 获取成功")
        else:
            debug_logger.log_error(f"[DrissionCaptcha] [Legacy] ❌ Token 获取失败")
        return token

    async def _create_resident_tab(self, project_id: str) -> Optional[ResidentTabInfo]:
        """为指定 project_id 创建常驻标签页

        模拟真实用户行为，依次访问：
        1. www.google.com (停留 3 秒)
        2. https://labs.google/fx/zh/tools/flow (停留 3 秒)
        3. 最终目标 URL

        Args:
            project_id: 项目 ID

        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[DrissionCaptcha] 为 project_id={project_id} 创建常驻标签页")

            # 创建新标签页（先访问 Google 首页）
            tab = await _run_in_thread(self._sync_new_tab, "https://www.google.com")
            await asyncio.sleep(3)
            debug_logger.log_info("[DrissionCaptcha] ✓ 已访问 www.google.com")

            # 访问 Flow 中文首页
            await _run_in_thread(self._sync_tab_get, tab, "https://labs.google/fx/zh/tools/flow")
            await asyncio.sleep(3)
            debug_logger.log_info("[DrissionCaptcha] ✓ 已访问 labs.google/fx/zh/tools/flow")

            # 最后访问目标 URL
            debug_logger.log_info(f"[DrissionCaptcha] 正在访问目标 URL: {website_url}")
            await _run_in_thread(self._sync_tab_get, tab, website_url)

            # 等待页面加载完成
            page_loaded = await self._wait_page_load(tab, timeout=60)

            if not page_loaded:
                debug_logger.log_error(f"[DrissionCaptcha] 页面加载超时 (project: {project_id})")
                await _run_in_thread(self._sync_close_tab, tab)
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error(f"[DrissionCaptcha] reCAPTCHA 加载失败 (project: {project_id})")
                await _run_in_thread(self._sync_close_tab, tab)
                return None

            # 创建常驻信息对象
            resident_info = ResidentTabInfo(tab, project_id)
            resident_info.recaptcha_ready = True

            debug_logger.log_info(f"[DrissionCaptcha] ✅ 常驻标签页创建成功 (project: {project_id})")
            return resident_info

        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 创建常驻标签页异常: {e}")
            return None

    async def _close_resident_tab(self, project_id: str):
        """关闭指定 project_id 的常驻标签页

        Args:
            project_id: 项目 ID
        """
        resident_info = self._resident_tabs.pop(project_id, None)
        if resident_info and resident_info.tab:
            await _run_in_thread(self._sync_close_tab, resident_info.tab)
            debug_logger.log_info(f"[DrissionCaptcha] 已关闭 project_id={project_id} 的常驻标签页")

    async def _get_token_legacy(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已启动
        if not self._initialized or not self.browser:
            await self.initialize()

        start_time = time.time()
        tab = None

        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[DrissionCaptcha] [Legacy] 访问页面: {website_url}")

            # 新建标签页并访问页面
            tab = await _run_in_thread(self._sync_new_tab, website_url)

            # 等待页面完全加载
            debug_logger.log_info("[DrissionCaptcha] [Legacy] 等待页面加载...")
            await asyncio.sleep(3)

            # 等待页面 DOM 完成
            page_loaded = await self._wait_page_load(tab, timeout=10)
            if not page_loaded:
                debug_logger.log_error("[DrissionCaptcha] [Legacy] 页面加载超时")
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error("[DrissionCaptcha] [Legacy] reCAPTCHA 无法加载")
                return None

            # 执行 reCAPTCHA
            debug_logger.log_info(f"[DrissionCaptcha] [Legacy] 执行 reCAPTCHA 验证 (action: {action})...")
            token = await self._execute_recaptcha_on_tab(tab, action)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[DrissionCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[DrissionCaptcha] [Legacy] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] [Legacy] 获取token异常: {str(e)}")
            return None
        finally:
            # 关闭标签页（但保留浏览器）
            if tab:
                await _run_in_thread(self._sync_close_tab, tab)

    async def close(self):
        """关闭浏览器"""
        # 停止心跳任务
        self._stop_heartbeat()

        # 先停止所有常驻模式（关闭所有常驻标签页）
        await self.stop_resident_mode()

        try:
            if self.browser:
                await _run_in_thread(self._sync_close_browser)
                self.browser = None

            self._initialized = False
            self._resident_tabs.clear()  # 确保清空常驻字典
            debug_logger.log_info("[DrissionCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 关闭浏览器异常: {str(e)}")

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        await _run_in_thread(lambda: self.browser.get("https://accounts.google.com/"))
        debug_logger.log_info("[DrissionCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ==================== Session Token 刷新 ====================

    async def refresh_session_token(self, project_id: str) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token

        复用 reCAPTCHA 常驻标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token

        Args:
            project_id: 项目ID，用于定位常驻标签页

        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        # 确保浏览器已初始化
        await self.initialize()

        start_time = time.time()
        debug_logger.log_info(f"[DrissionCaptcha] 开始刷新 Session Token (project: {project_id})...")

        # 尝试获取或创建常驻标签页
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)

            # 如果该 project_id 没有常驻标签页，则创建
            if resident_info is None:
                debug_logger.log_info(f"[DrissionCaptcha] project_id={project_id} 没有常驻标签页，正在创建...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[DrissionCaptcha] 无法为 project_id={project_id} 创建常驻标签页")
                    return None
                self._resident_tabs[project_id] = resident_info

        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[DrissionCaptcha] 无法获取常驻标签页")
            return None

        tab = resident_info.tab

        try:
            # 刷新页面以获取最新的 cookies
            debug_logger.log_info(f"[DrissionCaptcha] 刷新常驻标签页以获取最新 cookies...")
            await _run_in_thread(self._sync_reload_tab, tab)

            # 等待页面加载完成
            page_loaded = await self._wait_page_load(tab, timeout=30)
            if not page_loaded:
                debug_logger.log_error("[DrissionCaptcha] 刷新页面加载超时")
                return None

            # 额外等待确保 cookies 已设置
            await asyncio.sleep(2)

            # 从 cookies 中提取 __Secure-next-auth.session-token
            session_token = None

            try:
                # 使用 DrissionPage 的 cookies API（兼容多种返回格式）
                cookies = await _run_in_thread(self._sync_get_cookies)
                session_token = self._extract_session_token_from_cookies(cookies)

            except Exception as e:
                debug_logger.log_warning(f"[DrissionCaptcha] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")

                # 备选方案：通过 JavaScript 获取 - 需要加 return
                try:
                    all_cookies = await _run_in_thread(lambda: tab.run_js("return document.cookie"))
                    if all_cookies:
                        for part in all_cookies.split(";"):
                            part = part.strip()
                            if part.startswith("__Secure-next-auth.session-token="):
                                session_token = part.split("=", 1)[1]
                                break
                except Exception as e2:
                    debug_logger.log_error(f"[DrissionCaptcha] document.cookie 获取失败: {e2}")

            duration_ms = (time.time() - start_time) * 1000

            if session_token:
                debug_logger.log_info(f"[DrissionCaptcha] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                return session_token
            else:
                debug_logger.log_error(f"[DrissionCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None

        except Exception as e:
            debug_logger.log_error(f"[DrissionCaptcha] 刷新 Session Token 异常: {str(e)}")

            # 常驻标签页可能已失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # 重建后再次尝试获取
                    try:
                        cookies = await _run_in_thread(self._sync_get_cookies)
                        if isinstance(cookies, dict):
                            session_token = cookies.get("__Secure-next-auth.session-token")
                            if session_token:
                                debug_logger.log_info(f"[DrissionCaptcha] ✅ 重建后 Session Token 获取成功")
                                return session_token
                    except Exception:
                        pass

            return None

    # ==================== 状态查询 ====================

    def is_resident_mode_active(self) -> bool:
        """检查是否有任何常驻标签页激活"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """获取当前常驻标签页数量"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """获取所有当前常驻的 project_id 列表"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前常驻的 project_id（向后兼容，返回第一个）"""
        if self._resident_tabs:
            return next(iter(self._resident_tabs.keys()))
        return self.resident_project_id
