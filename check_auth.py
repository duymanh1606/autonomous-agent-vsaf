import asyncio

from load_config import load_config
from tools.browser import ChromeBrowserTool


async def main():
    config = load_config()
    browser = ChromeBrowserTool(
        cdp_url=config.get("browser.cdp_url", "http://127.0.0.1:29222"),
        base_url=config.get("platform.tiktok_web.base_url", "https://www.tiktok.com"),
        timeout_ms=config.get("browser.timeout_ms", 15000),
    )
    try:
        await browser.start()
        result = await browser.check_auth()
        print(result["auth_state"])
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
