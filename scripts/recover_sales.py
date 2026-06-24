import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.database import AsyncSessionFactory
from models import User, Subscription, XUIClientRecord, Server
from sqlalchemy import select

def extract_text(msg_text):
    if isinstance(msg_text, str):
        return msg_text
    elif isinstance(msg_text, list):
        out = ""
        for item in msg_text:
            if isinstance(item, str):
                out += item
            elif isinstance(item, dict) and "text" in item:
                out += item["text"]
        return out
    return ""

def strip_html(text):
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    # decode html entities like &quot; if any, but regex usually suffices
    return text

async def main():
    json_path = Path("result.json")
    html_path = Path("messages.html")
    
    if not json_path.exists() and not Path("/opt/telegramsellbot/result.json").exists():
        if not html_path.exists() and not Path("/opt/telegramsellbot/messages.html").exists():
            print("ERROR: result.json or messages.html not found. Please upload it to /opt/telegramsellbot/messages.html")
            sys.exit(1)
            
    purchases = []

    if Path("messages.html").exists() or Path("/opt/telegramsellbot/messages.html").exists():
        target_path = Path("messages.html") if Path("messages.html").exists() else Path("/opt/telegramsellbot/messages.html")
        print(f"Reading HTML file: {target_path}...")
        with open(target_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        chunks = content.split("خرید جدید!")
        for chunk in chunks[1:]:
            text = strip_html(chunk)
            user_id_match = re.search(r"\(ID:\s*(\d+)\)", text)
            telegram_id = int(user_id_match.group(1)) if user_id_match else None
            
            config_name_match = re.search(r"کانفیگ:\s*([a-zA-Z0-9_-]+)", text)
            config_name = config_name_match.group(1) if config_name_match else None
            
            if telegram_id and config_name:
                purchases.append({
                    "telegram_id": telegram_id,
                    "config_name": config_name,
                })
    else:
        target_path = Path("result.json") if Path("result.json").exists() else Path("/opt/telegramsellbot/result.json")
        print(f"Reading JSON file: {target_path}...")
        with open(target_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        messages = data.get("messages", [])

        for msg in messages:
            text = extract_text(msg.get("text", ""))
            if "خرید جدید!" not in text:
                continue
            
            user_id_match = re.search(r"\(ID:\s*(\d+)\)", text)
            telegram_id = int(user_id_match.group(1)) if user_id_match else None
            
            config_name_match = re.search(r"کانفیگ:\s*([a-zA-Z0-9_-]+)", text)
            config_name = config_name_match.group(1) if config_name_match else None
            
            if telegram_id and config_name:
                purchases.append({
                    "telegram_id": telegram_id,
                    "config_name": config_name,
                })
    
    print(f"Found {len(purchases)} 'خرید جدید!' messages.")

    if not purchases:
        print("Nothing to recover.")
        return

    async with AsyncSessionFactory() as session:
        # Load all servers to map clients
        servers = (await session.scalars(select(Server).where(Server.is_active == True))).all()
        
        # Build a huge dictionary of ALL clients currently on X-UI servers
        print("Fetching all clients from X-UI servers to find missing configs...")
        all_xui_clients = {}
        
        for server in servers:
            if server.panel_type != "xui":
                continue
                
            from services.xui.client import SanaeiXUIClient, XUIClientConfig
            from pydantic import SecretStr
            
            config = XUIClientConfig(
                base_url=server.api_url,
                username=server.api_username,
                password=SecretStr(server.api_password),
                verify_ssl=False
            )
            
            try:
                async with SanaeiXUIClient(config) as client:
                    await client.login()
                    inbounds = await client.get_inbounds()
                    for inbound in inbounds:
                        settings = json.loads(inbound.settings)
                        for c in settings.get("clients", []):
                            email = c.get("email")
                            if email:
                                all_xui_clients[email] = {
                                    "server_id": server.id,
                                    "inbound_id": inbound.id,
                                    "client_dict": c,
                                    "server_obj": server
                                }
            except Exception as e:
                print(f"Error fetching from server {server.name}: {e}")
        
        print(f"Fetched {len(all_xui_clients)} unique clients from X-UI panels.")

        recovered_count = 0
        
        for p in purchases:
            telegram_id = p["telegram_id"]
            config_name = p["config_name"]
            
            # Check if this config already exists in our database
            existing_record = (await session.scalars(
                select(XUIClientRecord).where(XUIClientRecord.email == config_name)
            )).first()
            
            if existing_record:
                # Already exists, skip
                continue
                
            print(f"Missing config found in DB: {config_name} (User: {telegram_id})")
            
            xui_info = all_xui_clients.get(config_name)
            if not xui_info:
                print(f"  -> WARNING: {config_name} is missing from DB *and* not found on any X-UI server! Maybe deleted?")
                continue
                
            client_dict = xui_info["client_dict"]
            server_obj = xui_info["server_obj"]
            inbound_id = xui_info["inbound_id"]
            
            # Fetch client traffic to get volume and expiry
            from services.xui.client import SanaeiXUIClient, XUIClientConfig
            from pydantic import SecretStr
            config = XUIClientConfig(
                base_url=server_obj.api_url,
                username=server_obj.api_username,
                password=SecretStr(server_obj.api_password),
                verify_ssl=False
            )
            
            try:
                async with SanaeiXUIClient(config) as client:
                    await client.login()
                    traffic = await client.get_client_traffic(config_name)
                    
                    # Ensure User exists
                    user = (await session.scalars(select(User).where(User.telegram_id == telegram_id))).first()
                    if not user:
                        print(f"  -> Creating missing User {telegram_id}")
                        user = User(telegram_id=telegram_id, first_name="Recovered")
                        session.add(user)
                        await session.flush()
                        
                    # Create Subscription
                    total_bytes = traffic.total
                    used_bytes = traffic.up + traffic.down
                    expiry_time = traffic.expiryTime
                    
                    sub = Subscription(
                        user_id=user.id,
                        status="active" if traffic.enable else "expired",
                        volume_bytes=total_bytes,
                        used_bytes=used_bytes,
                        lifetime_used_bytes=0,
                        sub_link=client_dict.get("subId", str(uuid.uuid4()))
                    )
                    
                    if expiry_time and expiry_time > 0:
                        sub.ends_at = datetime.fromtimestamp(expiry_time / 1000, tz=timezone.utc)
                    else:
                        sub.ends_at = None
                        
                    session.add(sub)
                    await session.flush()
                    
                    # Create XUIClientRecord
                    record = XUIClientRecord(
                        subscription_id=sub.id,
                        server_id=server_obj.id,
                        xui_inbound_id=inbound_id,
                        email=config_name,
                        uuid=client_dict.get("id"),
                        sub_id=client_dict.get("subId"),
                        volume_bytes=total_bytes
                    )
                    session.add(record)
                    await session.commit()
                    
                    print(f"  -> Successfully recovered {config_name} -> Subscription {sub.id}")
                    recovered_count += 1
                    
            except Exception as e:
                print(f"  -> Error recovering {config_name}: {e}")
                await session.rollback()

        print(f"\nRecovery complete. Recovered {recovered_count} subscriptions.")

if __name__ == "__main__":
    asyncio.run(main())
