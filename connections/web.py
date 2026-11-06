"""Loopback-only OAuth application. Deploy behind a dedicated HTTPS reverse proxy."""
from __future__ import annotations

import logging
import os
import secrets
import time
from collections import OrderedDict

from aiohttp import web
from .security import ConnectionError
from .service import ConnectedAccountService

HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Strict-Transport-Security": "max-age=31536000",
}


def page(text, status=200):
    # Only application-owned constant strings, never provider-controlled values.
    return web.Response(text=text, content_type="text/plain", status=status, headers=HEADERS)


def create_app(service):
    service.settings.validate()
    rates = OrderedDict()

    @web.middleware
    async def boundary(request, handler):
        now = time.monotonic()
        key = request.remote or "unknown"  # Do not trust arbitrary X-Forwarded-For headers.
        start, count = rates.get(key, (now, 0))
        if now - start > 60:
            start, count = now, 0
        rates[key] = (start, count + 1)
        rates.move_to_end(key)
        while len(rates) > 512:
            rates.popitem(last=False)
        if count >= 120:
            return page("Please wait before trying again.", 429)
        try:
            return await handler(request)
        except ConnectionError as exc:
            if exc.code == "CANCELLED":
                return page("Google authorization was cancelled. No account was connected.", 400)
            return page("This connection could not be completed. Return to Discord and request a new link.", 400)
        except web.HTTPException:
            return page("Not found.", 404)
        except Exception:
            # Never log request URLs, query strings, provider bodies, codes or traceback locals.
            return page("The connection service is unavailable. Return to Discord and try again later.", 503)

    app = web.Application(middlewares=[boundary], client_max_size=8192)

    async def start(request):
        browser = secrets.token_urlsafe(32)
        url = await service.begin(request.match_info["token"], browser)
        response = web.Response(status=303, headers={**HEADERS, "Location": url})
        response.set_cookie("__Host-connections", browser, secure=True, httponly=True,
                            samesite="Lax", max_age=600, path="/")
        return response

    async def callback(request):
        if len(request.query_string) > 8192 or any(len(request.query.getall(k)) != 1 for k in request.query):
            raise ConnectionError("INVALID_SESSION")
        if request.query.get("iss", "https://accounts.google.com") != "https://accounts.google.com":
            raise ConnectionError("INVALID_SESSION")
        await service.callback(state=request.query.get("state", ""), code=request.query.get("code", ""),
                               error=request.query.get("error", ""), browser=request.cookies.get("__Host-connections", ""))
        response = page("Google approved the request. Return to the requesting bot's Discord DM and confirm the account link. No bot can use this new account until you confirm.")
        response.del_cookie("__Host-connections", path="/", secure=True, httponly=True, samesite="Lax")
        return response

    async def health(request):
        return page("ok")

    app.router.add_get("/health", health)
    app.router.add_get("/oauth/google/start/{token}", start)
    app.router.add_get("/oauth/google/callback", callback)
    return app


def main():
    path = os.environ.get("CONNECTIONS_SHARED_DB", "")
    if not path or not os.path.isabs(path):
        raise SystemExit("CONNECTIONS_SHARED_DB must be the absolute canonical shared database path")
    try:
        app = create_app(ConnectedAccountService(path))
    except ConnectionError:
        raise SystemExit("Connection service configuration is incomplete or invalid") from None
    logging.getLogger("aiohttp.access").disabled = True
    web.run_app(app, host="127.0.0.1", port=8787, access_log=None, print=None)


if __name__ == "__main__":
    main()
