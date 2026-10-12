"""Keep redirects from downgrading a configured HTTPS panel connection."""
import httpx


def enforce_https_redirects(client, base_url, error_type):
    if not isinstance(client, httpx.AsyncClient) or httpx.URL(base_url).scheme != "https":
        return

    async def require_https(request):
        if request.url.scheme != "https":
            raise error_type("Panel redirected HTTPS to an insecure connection; check the reverse proxy and canonical URL.")

    client.event_hooks["request"].append(require_https)
