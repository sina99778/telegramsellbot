from fastapi import APIRouter
from fastapi.responses import RedirectResponse
import urllib.parse

router = APIRouter()

@router.get("/dl/{app}")
async def deep_link(app: str, url: str):
    """
    Redirects HTTP links to application deep links since Telegram inline keyboards
    do not support custom schemes.
    """
    decoded_url = urllib.parse.unquote(url)
    
    if app == "v2rayng":
        target = f"v2rayng://install-config?url={url}"
    elif app == "shadowrocket":
        target = f"shadowrocket://install-sub?url={url}"
    elif app == "v2box":
        target = f"v2box://install-sub?url={url}"
    else:
        target = f"{app}://install-sub?url={url}"
        
    return RedirectResponse(url=target)
