"""AI Studio popup handling shared by startup and keepalive."""

import re
import time

from playwright.sync_api import Page

from browser.ws_helper import click_connect, get_ws_status


DIALOG_SELECTOR = (
    'mat-mdc-dialog-container, .cdk-overlay-pane, [role="dialog"], '
    '[aria-modal="true"], ms-g1-welcome-dialog, [data-aistudiobuildws-popup]'
)
ONBOARDING_TITLE = "An updated flow for using Gemini in your apps"
URL_PROMO_TITLE = "Grab your custom URL today!"
POPUP_ACTIONS = (
    "Skip", "Continue to the app", "Continue to app", "Got it, thanks",
    "Got it", "Dismiss", "OK", "Agree", "Accept", "I agree", "Continue",
    "Connect", "确认连接", "连接", "继续", "Done", "Next",
)


class AppReadinessError(Exception):
    pass


class PreviewAuthenticationError(AppReadinessError):
    """The Google error document rendered in place of the app Preview."""


def _check_preview_authentication(page: Page, deadline):
    # Error documents may use a different iframe title than the working app.
    # Inspect rendered frames instead of relying on iframe[title="Preview"].
    for frame in page.frames:
        if time.monotonic() >= deadline:
            return
        try:
            current = frame
            visible = True
            while current.parent_frame is not None:
                element = current.frame_element()
                try:
                    visible = element.is_visible()
                finally:
                    element.dispose()
                if not visible:
                    break
                current = current.parent_frame
            if not visible:
                continue
            remaining_ms = max(1, min(250, (deadline - time.monotonic()) * 1000))
            text = frame.locator("body").inner_text(timeout=remaining_ms)
            normalized = " ".join(text.replace("’", "'").split())
            if not (
                re.match(r"^(?:Google\s+)?401\.\s+That's an error\.", normalized)
                and "The server cannot process the request because it is malformed." in normalized
                and "It should not be retried." in normalized
                and "That's all we know." in normalized
            ):
                continue
            branded = frame.locator(
                '[aria-label="Google"], img[alt="Google"], '
                'a[href="//www.google.com/"], a[href="https://www.google.com/"]'
            ).count() or normalized.startswith("Google ") or "!!1" in frame.title()
            if branded:
                raise PreviewAuthenticationError(
                    "Google Preview 返回 401 错误页，应用未启动；"
                    "请使用正常浏览器中可加载此 Preview 的账号重新导出 JSON Cookie，"
                    "替换对应 USER_COOKIE 环境变量后重建容器以加载凭证。"
                    "此错误发生在 Preview 文档加载阶段，尚未运行应用内的 WebSocket。"
                )
        except PreviewAuthenticationError:
            raise
        except Exception:
            # A frame can be detached/replaced while the editor renders.
            continue


def _visible_dialogs(page: Page):
    dialogs = []
    containers = page.locator(DIALOG_SELECTOR)
    for index in range(containers.count()):
        container = containers.nth(index)
        if container.is_visible():
            dialogs.append(container)

    # Some AI Studio announcements have no dialog role/Material wrapper.
    # Anchor to their known heading, never to generic page-wide Skip/Next.
    for title in (ONBOARDING_TITLE, URL_PROMO_TITLE):
        headings = page.get_by_text(title, exact=True)
        for index in range(headings.count()):
            heading = headings.nth(index)
            if not heading.is_visible():
                continue
            container = heading.locator(
                "xpath=ancestor::*[not(self::body) and not(self::html)]"
                "[.//button or .//*[@role='button']][1]"
            )
            if container.count() and container.is_visible():
                # Keep recognizing an unlabelled tour when Next changes its
                # heading but leaves the same popup container on screen.
                container.evaluate(
                    "(element, kind) => element.setAttribute('data-aistudiobuildws-popup', kind)",
                    "promo" if title == URL_PROMO_TITLE else "onboarding",
                )
                dialogs.append(container)
            else:
                # An announcement without an actionable container still blocks
                # readiness; don't turn a missing close control into success.
                dialogs.append(heading)
    return dialogs


def has_visible_dialog(page: Page) -> bool:
    return bool(_visible_dialogs(page))


def _click_action(button, logger=None, timeout_ms=1000) -> bool:
    try:
        if not button.is_visible() or not button.is_enabled():
            return False
        action_deadline = time.monotonic() + timeout_ms / 1000
        button.scroll_into_view_if_needed(timeout=timeout_ms)
        # A visible lower dialog can still be covered by another overlay.
        # Check the actual hit target and let Playwright recheck before clicking.
        if not button.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            const target = document.elementFromPoint(
                rect.x + rect.width / 2, rect.y + rect.height / 2
            );
            return target === element || element.contains(target);
        }"""):
            return False
        remaining_ms = (action_deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            return False
        button.click(timeout=remaining_ms)
        return True
    except Exception as error:
        if logger:
            logger.debug(f"弹窗按钮暂不可点击: {error}")
        return False


def dismiss_popups(page: Page, logger=None, max_iterations=8, deadline=None) -> bool:
    """Dismiss visible overlays; return False if an overlay is still blocking."""
    for _ in range(max_iterations):
        if deadline is not None and time.monotonic() >= deadline:
            break
        dialogs = _visible_dialogs(page)
        if not dialogs:
            return True
        clicked = False
        for dialog in reversed(dialogs):
            if dialog.get_attribute("data-aistudiobuildws-popup") == "promo" or dialog.get_by_text(URL_PROMO_TITLE, exact=True).count():
                # Close this promotion instead of entering URL publishing.
                actions = [dialog.get_by_role(
                    "button", name=re.compile(r"^(close|dismiss)(?:\s+.*)?$", re.I)
                ), dialog.locator(
                    'button:has(mat-icon:text-is("close")), '
                    'button:has(.material-icons:text-is("close")), '
                    'button:text-is("×"), button:text-is("✕"), '
                    'button:text-is("X"), button:text-is("close")'
                )]
            else:
                actions = [dialog.get_by_role("button", name=name, exact=True)
                           for name in POPUP_ACTIONS]
            for action in actions:
                for index in range(action.count()):
                    remaining_ms = 1000 if deadline is None else min(1000, (deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        return False
                    if _click_action(action.nth(index), logger, timeout_ms=remaining_ms):
                        clicked = True
                        if logger:
                            logger.info("已点击 AI Studio 弹窗内的关闭/继续按钮")
                        break
                if clicked:
                    break
            if clicked:
                break
        if not clicked:
            return False
        # Rescan after every action: it can reveal another dialog or tour step.
        pause_ms = 150 if deadline is None else min(150, max(0, deadline - time.monotonic()) * 1000)
        page.wait_for_timeout(pause_ms)
    return not has_visible_dialog(page)


def wait_for_app_ready(page: Page, logger=None, timeout=30) -> str:
    """Require a clear, rendered Preview and an established WS connection."""
    started_at = time.monotonic()
    deadline = started_at + timeout
    clear_since = None
    next_connect_at = 0
    last_status = "UNKNOWN"
    preview_visible = False
    blocking = False
    auth_error = None
    auth_error_since = None
    last_observation = None
    while time.monotonic() < deadline:
        # One action per poll keeps repeated buttons within the real deadline.
        # The handler already rescans after clicking; reuse its result instead
        # of repeating all dialog lookups on a busy browser.
        blocking = not dismiss_popups(page, logger, max_iterations=1, deadline=deadline)
        try:
            if not blocking:
                _check_preview_authentication(page, deadline)
            auth_error = None
            auth_error_since = None
        except PreviewAuthenticationError as error:
            auth_error = error
            if auth_error_since is None:
                auth_error_since = time.monotonic()
            # Let onboarding/auth bootstrap replace a transient error document.
            if time.monotonic() - auth_error_since >= 1:
                raise
        preview = page.locator('iframe[title="Preview"]').first
        preview_visible = preview.is_visible()
        last_status = get_ws_status(page, logger) if preview_visible else "UNKNOWN"
        now = time.monotonic()
        observation = (blocking, preview_visible, last_status, auth_error is not None)
        if logger and observation != last_observation:
            logger.info(
                f"AI Studio 就绪检查 ({now - started_at:.1f}s): "
                f"blocking_dialog={blocking}, preview_visible={preview_visible}, "
                f"WS={last_status}, preview_auth_error={auth_error is not None}"
            )
        last_observation = observation
        clear_preview = not blocking and preview_visible and auth_error is None
        if clear_preview and last_status == "CONNECTED":
            # Editor/chat progress indicators can remain active after the app
            # connects. The rendered Preview's WS state establishes readiness.
            if clear_since is None:
                clear_since = now
            # Catch asynchronously mounted onboarding before declaring success.
            # Assess a completed observation before timing out: slow browser
            # calls can finish just beyond the budget after an earlier good poll.
            if now - clear_since >= 1:
                blocking = has_visible_dialog(page)
                if not blocking:
                    return last_status
                # A dialog can mount while the Preview/status reads are running.
                clear_since = None
                clear_preview = False
                now = time.monotonic()
        else:
            clear_since = None
        if now >= deadline:
            break
        if clear_preview:
            if last_status in ("IDLE", "DISCONNECTED", "ERROR") and now >= next_connect_at:
                click_connect(page, logger, timeout_ms=min(1000, (deadline - now) * 1000))
                next_connect_at = time.monotonic() + 5
        page.wait_for_timeout(min(200, max(0, deadline - time.monotonic()) * 1000))
    if auth_error is not None:
        raise auth_error
    clear_duration = 0 if clear_since is None else time.monotonic() - clear_since
    raise AppReadinessError(
        f"AI Studio 未准备就绪: blocking_dialog={blocking}, "
        f"preview_visible={preview_visible}, WS={last_status}, "
        f"connected_clear_for={clear_duration:.1f}s"
    )
